#!/usr/bin/env python3
"""Generador determinista de escenarios de carga del experimento.

Un escenario es la lista de versiones sintéticas de una corrida (rama release/c<N>-<n>), con su tipo,
sus repos, su migración propia, su resultado y la línea de tiempo de aperturas y actualizaciones.
Solo usa la biblioteca estándar. Los mismos (corrida, nivel, semilla) dan siempre el mismo JSON.

    python escenario.py --corrida N --nivel C --semilla S --salida escenarios/c<N>.json
    python escenario.py --ver escenarios/c<N>.json
"""

import argparse
import functools
import json
import random
import subprocess
import sys
from pathlib import Path

VERSION_FORMATO = 1

NIVELES = (1, 3, 5)
BRECHA = (0, 600)  # segundos, enteros, extremos incluidos

# Orden fijo: es el orden en que se recorre la distribución acumulada en cada sorteo.
TIPOS = {
    "solo_frontend": 0.19,
    "solo_backend": 0.23,
    "migracion": 0.41,
    "combinacion": 0.17,
}
REPOS_POR_TIPO = {
    "solo_frontend": ["frontend"],
    "solo_backend": ["backend"],
    "migracion": ["backend"],
    "combinacion": ["backend", "frontend"],
}
TIPOS_CON_MIGRACION = ("migracion", "combinacion")
ACTUALIZACIONES = {0: 0.57, 1: 0.28, 2: 0.08, 3: 0.07}
RESULTADOS = {"integrada": 0.75, "declinada": 0.25}
PRIMER_REPO_COMBINACION = 0.5  # probabilidad de que el backend abra primero

PARAMETROS = {
    "niveles": list(NIVELES),
    "brecha_apertura_seg": list(BRECHA),
    "tipos": TIPOS,
    "repos_por_tipo": REPOS_POR_TIPO,
    "tipos_con_migracion": list(TIPOS_CON_MIGRACION),
    "combinacion_primer_repo_backend": PRIMER_REPO_COMBINACION,
    "brecha_segundo_repo_seg": list(BRECHA),
    "actualizaciones": {str(k): v for k, v in ACTUALIZACIONES.items()},
    "brecha_actualizacion_seg": list(BRECHA),
    "resultados": RESULTADOS,
}


def _sortear(rng, distribucion):
    """Sorteo categórico con un único rng.random(), recorriendo las claves en orden."""
    x = rng.random()
    acumulada = 0.0
    ultima = None
    for valor, p in distribucion.items():
        acumulada += p
        ultima = valor
        if x < acumulada:
            return valor
    return ultima  # x en el residuo de redondeo de la suma


def _brecha(rng):
    return rng.randint(*BRECHA)


@functools.lru_cache(maxsize=1)
def commit_generador():
    """SHA del HEAD del repo del generador, o "sin-commit" si no se puede leer."""
    try:
        salida = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        )
        sha = salida.stdout.strip()
        return sha if len(sha) == 40 else "sin-commit"
    except (OSError, subprocess.CalledProcessError):
        return "sin-commit"


def generar(corrida: int, nivel: int, semilla: int) -> dict:
    if nivel not in NIVELES:
        raise ValueError(f"nivel tiene que ser uno de {NIVELES} (es {nivel!r})")
    if isinstance(corrida, bool) or not isinstance(corrida, int) or corrida < 0:
        raise ValueError(f"corrida tiene que ser un entero >= 0 (es {corrida!r})")

    rng = random.Random(semilla)
    versiones = []
    apertura = 0

    # Orden de los sorteos, por versión n = 1..nivel:
    #   1. brecha de apertura (solo n > 1)  2. tipo  3. combinación: primer repo y brecha del segundo
    #   4. cantidad de actualizaciones  5. por actualización: brecha y repo  6. resultado
    for n in range(1, nivel + 1):
        if n > 1:
            apertura += _brecha(rng)
        tipo = _sortear(rng, TIPOS)
        repos = list(REPOS_POR_TIPO[tipo])

        aperturas = {}  # repo -> t de apertura
        if tipo == "combinacion":
            primero = "backend" if rng.random() < PRIMER_REPO_COMBINACION else "frontend"
            segundo = "frontend" if primero == "backend" else "backend"
            aperturas[primero] = apertura
            aperturas[segundo] = apertura + _brecha(rng)
        else:
            aperturas[repos[0]] = apertura

        contador = {repo: 0 for repo in repos}
        eventos = [
            {"t": t, "repo": repo, "accion": "apertura", "actualizacion": 0}
            for repo, t in sorted(aperturas.items(), key=lambda par: par[1])
        ]

        # Cada actualización va a una brecha del evento anterior de la cadena (la apertura de la
        # versión para la primera) y a un repo de la versión que ya abrió en ese instante.
        anterior = apertura
        for _ in range(_sortear(rng, ACTUALIZACIONES)):
            t = anterior + _brecha(rng)
            abiertos = [repo for repo in repos if aperturas[repo] <= t]
            repo = rng.choice(abiertos)
            contador[repo] += 1
            eventos.append({"t": t, "repo": repo, "accion": "actualizacion", "actualizacion": contador[repo]})
            anterior = t

        # Por t; ante empate, la apertura antes que la actualización (sort estable).
        eventos.sort(key=lambda e: (e["t"], 0 if e["accion"] == "apertura" else 1))

        versiones.append(
            {
                "n": n,
                "rama": f"release/c{corrida}-{n}",
                "tipo": tipo,
                "repos": repos,
                "migracion": f"c{corrida}_{n}" if tipo in TIPOS_CON_MIGRACION else None,
                "resultado": _sortear(rng, RESULTADOS),
                "eventos": eventos,
            }
        )

    eventos_ordenados = sorted(
        ({"n": v["n"], **e} for v in versiones for e in v["eventos"]),
        key=lambda e: (e["t"], e["n"]),
    )

    return {
        "corrida": corrida,
        "nivel": nivel,
        "semilla": semilla,
        "generador": {"commit": commit_generador(), "version_formato": VERSION_FORMATO},
        "parametros": PARAMETROS,
        "catalogo_migraciones": [v["migracion"] for v in versiones if v["migracion"]],
        "versiones": versiones,
        "eventos_ordenados": eventos_ordenados,
    }


def a_json(escenario: dict) -> str:
    return json.dumps(escenario, indent=2, ensure_ascii=False) + "\n"


def _mmss(t):
    return f"{t // 60:3d}:{t % 60:02d}"


def linea_de_tiempo(escenario: dict) -> str:
    c = escenario["corrida"]
    lineas = [
        f"corrida c{c}  nivel {escenario['nivel']}  semilla {escenario['semilla']}  "
        f"generador {escenario['generador']['commit'][:7]} (formato {escenario['generador']['version_formato']})",
        f"migraciones: {', '.join(escenario['catalogo_migraciones']) or '-'}",
        "",
        "versiones:",
    ]
    for v in escenario["versiones"]:
        actualizaciones = sum(1 for e in v["eventos"] if e["accion"] == "actualizacion")
        lineas.append(
            f"  {v['rama']:<16} {v['tipo']:<14} {'+'.join(v['repos']):<17} "
            f"migracion {v['migracion'] or '-':<6} {actualizaciones} act.  {v['resultado']}"
        )
    lineas += ["", "    t   mm:ss  versión  repo      acción          k"]
    for e in escenario["eventos_ordenados"]:
        lineas.append(
            f"  {e['t']:5d}  {_mmss(e['t'])}  c{c}-{e['n']:<5} {e['repo']:<9} {e['accion']:<14}  {e['actualizacion']}"
        )
    fin = escenario["eventos_ordenados"][-1]["t"] if escenario["eventos_ordenados"] else 0
    lineas += ["", f"último evento en t = {fin} s ({_mmss(fin).strip()})"]
    return "\n".join(lineas)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generador determinista de escenarios de carga")
    parser.add_argument("--corrida", type=int)
    parser.add_argument("--nivel", type=int, choices=NIVELES)
    parser.add_argument("--semilla", type=int)
    parser.add_argument("--salida", help="archivo JSON a escribir (por ejemplo escenarios/c1.json)")
    parser.add_argument("--ver", metavar="ARCHIVO", help="imprime la línea de tiempo de un escenario")
    args = parser.parse_args(argv)

    if args.ver:
        if any(x is not None for x in (args.corrida, args.nivel, args.semilla, args.salida)):
            parser.error("--ver no se combina con --corrida, --nivel, --semilla ni --salida")
        print(linea_de_tiempo(json.loads(Path(args.ver).read_text(encoding="utf-8"))))
        return 0

    faltan = [f"--{k}" for k in ("corrida", "nivel", "semilla", "salida") if getattr(args, k) is None]
    if faltan:
        parser.error("faltan " + ", ".join(faltan) + " (o usar --ver ARCHIVO)")
    try:
        escenario = generar(args.corrida, args.nivel, args.semilla)
    except ValueError as error:
        parser.error(str(error))
    salida = Path(args.salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(a_json(escenario), encoding="utf-8")
    print(f"escenario c{args.corrida} (nivel {args.nivel}, semilla {args.semilla}) en {salida}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
