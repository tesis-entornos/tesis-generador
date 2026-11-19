#!/usr/bin/env python3
"""Generador determinista de escenarios de carga del experimento.

Un escenario es la lista de versiones sintéticas de una corrida (rama release/c<N>-<n>), con su tipo,
sus repos, su migración propia, su resultado y la línea de tiempo de aperturas y actualizaciones.
Solo usa la biblioteca estándar. Los mismos (corrida, nivel, semilla[, actualizaciones_fijas, tipo_fijo,
brecha_actualizacion_seg, brecha_segundo_repo_seg]) dan siempre el mismo JSON.

    python escenario.py --corrida N --nivel C --semilla S [--actualizaciones-fijas K] [--tipo-fijo T]
        [--brecha-actualizacion-seg B] [--brecha-segundo-repo-seg B] --salida escenarios/c<N>.json
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
# Calibración medida; el experimento definitivo baja P(0) a la mitad y reparte el resto en la proporción medida.
ACTUALIZACIONES_CALIBRACION = {0: 0.57, 1: 0.28, 2: 0.08, 3: 0.07}
ACTUALIZACIONES_REGLA = "P(0) a la mitad de la calibración; resto en la proporción medida"


def _actualizaciones(calibracion):
    p0 = calibracion[0] / 2
    resto = sum(p for k, p in calibracion.items() if k != 0)
    return {k: (p0 if k == 0 else (1 - p0) * p / resto) for k, p in calibracion.items()}


ACTUALIZACIONES = _actualizaciones(ACTUALIZACIONES_CALIBRACION)
# Diseño del tutor (17/11): una corrida puede fijar la cantidad de actualizaciones de TODAS sus versiones en k; ese k
# reemplaza el sorteo de la cantidad (no se consume ese rng.random()). Sin el parámetro, se sortea con ACTUALIZACIONES.
ACTUALIZACIONES_FIJAS = (0, 1, 2)
ACTUALIZACIONES_FIJAS_REGLA = ("parámetro por corrida (escenario.actualizaciones_fijas); si está, toda versión tiene "
                               "exactamente k actualizaciones y no se sortea la cantidad; aperturas, composición, repo y "
                               "brecha de cada actualización y resultado se sortean igual")
# Matriz completa (Martin con el tutor, 19/11): una corrida puede fijar el tipo de TODAS sus versiones (tipo_fijo) y
# las brechas de las actualizaciones y de la segunda apertura de las combinaciones (en segundos). Cada parámetro fijado
# reemplaza su sorteo (no se consume ese rng); sin los parámetros, todo se sortea como antes.
TIPOS_FIJOS = ("solo_backend", "solo_frontend", "combinacion")
TIPO_FIJO_REGLA = ("parámetro por corrida (escenario.tipo_fijo); si está, toda versión es de ese tipo y no se sortea el "
                   "tipo; en combinacion se sortean igual el primer repo, el repo de cada actualización entre los abiertos "
                   "y el resultado")
BRECHA_ACTUALIZACION_FIJA_REGLA = ("parámetro por corrida (escenario.brecha_actualizacion_seg); si está, toda "
                                   "actualización va a esa brecha (s) del evento anterior de la cadena y no se sortea")
BRECHA_SEGUNDO_REPO_FIJA_REGLA = ("parámetro por corrida (escenario.brecha_segundo_repo_seg); si está, el segundo repo "
                                  "de cada combinación abre a esa brecha (s) del primero y no se sortea")
# Fuera de PARAMETROS (como actualizaciones_fijas en el JSON): así los escenarios anteriores se regeneran idénticos.
# El manifiesto del protocolo las registra aparte (protocolo.parametros_escenario).
REGLAS_FIJAS = {
    "tipo_fijo": {"valores": list(TIPOS_FIJOS), "regla": TIPO_FIJO_REGLA},
    "brecha_actualizacion_seg": {"rango": list(BRECHA), "regla": BRECHA_ACTUALIZACION_FIJA_REGLA},
    "brecha_segundo_repo_seg": {"rango": list(BRECHA), "regla": BRECHA_SEGUNDO_REPO_FIJA_REGLA},
}
RESULTADOS = {"integrada": 0.75, "declinada": 0.25}
PRIMER_REPO_COMBINACION = 0.5  # probabilidad de que el backend abra primero

PARAMETROS = {
    "niveles": list(NIVELES),
    "brecha_apertura_seg": list(BRECHA),
    "aperturas": "ventana única: primera apertura en t=0, resto uniforme en (0, 600) s",
    "tipos": TIPOS,
    "repos_por_tipo": REPOS_POR_TIPO,
    "tipos_con_migracion": list(TIPOS_CON_MIGRACION),
    "combinacion_primer_repo_backend": PRIMER_REPO_COMBINACION,
    "brecha_segundo_repo_seg": list(BRECHA),
    "actualizaciones": {str(k): v for k, v in ACTUALIZACIONES.items()},
    "actualizaciones_calibracion": {str(k): v for k, v in ACTUALIZACIONES_CALIBRACION.items()},
    "actualizaciones_regla": ACTUALIZACIONES_REGLA,
    "brecha_actualizacion_seg": list(BRECHA),
    "resultados": RESULTADOS,
    "actualizaciones_fijas": {"valores": list(ACTUALIZACIONES_FIJAS), "regla": ACTUALIZACIONES_FIJAS_REGLA},
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


def _brecha_fija(nombre, valor):
    if valor is not None and (type(valor) is not int or not BRECHA[0] <= valor <= BRECHA[1]):
        raise ValueError(f"{nombre} tiene que ser None o un entero en [{BRECHA[0]}, {BRECHA[1]}] (es {valor!r})")


def generar(corrida: int, nivel: int, semilla: int, actualizaciones_fijas=None, tipo_fijo=None,
            brecha_actualizacion_seg=None, brecha_segundo_repo_seg=None) -> dict:
    """actualizaciones_fijas: None (se sortea la cantidad por versión, como hasta v4) o k en ACTUALIZACIONES_FIJAS
    (toda versión tiene exactamente k actualizaciones; el JSON registra el k en "actualizaciones_fijas").
    tipo_fijo: None (se sortea el tipo) o uno de TIPOS_FIJOS (toda versión es de ese tipo).
    brecha_actualizacion_seg / brecha_segundo_repo_seg: None (se sortean en BRECHA) o un entero fijo en BRECHA.
    Cada parámetro dado queda registrado con su nombre en el JSON, fuera de "parametros"."""
    if nivel not in NIVELES:
        raise ValueError(f"nivel tiene que ser uno de {NIVELES} (es {nivel!r})")
    if isinstance(corrida, bool) or not isinstance(corrida, int) or corrida < 0:
        raise ValueError(f"corrida tiene que ser un entero >= 0 (es {corrida!r})")
    if actualizaciones_fijas is not None and (
        type(actualizaciones_fijas) is not int or actualizaciones_fijas not in ACTUALIZACIONES_FIJAS
    ):
        raise ValueError(f"actualizaciones_fijas tiene que ser None o uno de {ACTUALIZACIONES_FIJAS} (es {actualizaciones_fijas!r})")
    if tipo_fijo is not None and (type(tipo_fijo) is not str or tipo_fijo not in TIPOS_FIJOS):
        raise ValueError(f"tipo_fijo tiene que ser None o uno de {TIPOS_FIJOS} (es {tipo_fijo!r})")
    _brecha_fija("brecha_actualizacion_seg", brecha_actualizacion_seg)
    _brecha_fija("brecha_segundo_repo_seg", brecha_segundo_repo_seg)

    rng = random.Random(semilla)
    versiones = []
    # Aperturas en una ventana única: la primera en t = 0 y las otras nivel - 1 en instantes de BRECHA,
    # sorteados juntos al principio y ordenados de menor a mayor.
    instantes = [0] + sorted(_brecha(rng) for _ in range(nivel - 1))

    # Orden de los sorteos: 0. los nivel - 1 instantes de apertura; después, por versión n = 1..nivel:
    #   1. tipo (no se sortea si la corrida lo fija)  2. combinación: primer repo y brecha del segundo (no se sortea
    #   si la corrida la fija)  3. cantidad de actualizaciones (no se sortea si la corrida la fija)
    #   4. por actualización: brecha (no se sortea si la corrida la fija) y repo  5. resultado
    for n in range(1, nivel + 1):
        apertura = instantes[n - 1]
        tipo = _sortear(rng, TIPOS) if tipo_fijo is None else tipo_fijo
        repos = list(REPOS_POR_TIPO[tipo])

        aperturas = {}  # repo -> t de apertura
        if tipo == "combinacion":
            primero = "backend" if rng.random() < PRIMER_REPO_COMBINACION else "frontend"
            segundo = "frontend" if primero == "backend" else "backend"
            aperturas[primero] = apertura
            aperturas[segundo] = apertura + (_brecha(rng) if brecha_segundo_repo_seg is None else brecha_segundo_repo_seg)
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
        cantidad = _sortear(rng, ACTUALIZACIONES) if actualizaciones_fijas is None else actualizaciones_fijas
        for _ in range(cantidad):
            t = anterior + (_brecha(rng) if brecha_actualizacion_seg is None else brecha_actualizacion_seg)
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

    salida = {"corrida": corrida, "nivel": nivel, "semilla": semilla}
    fijos = {"actualizaciones_fijas": actualizaciones_fijas, "tipo_fijo": tipo_fijo,
             "brecha_actualizacion_seg": brecha_actualizacion_seg, "brecha_segundo_repo_seg": brecha_segundo_repo_seg}
    for clave, valor in fijos.items():  # sin el parámetro, el JSON es el de siempre (sin esa clave)
        if valor is not None:
            salida[clave] = valor
    salida.update(
        {
            "generador": {"commit": commit_generador(), "version_formato": VERSION_FORMATO},
            "parametros": PARAMETROS,
            "catalogo_migraciones": [v["migracion"] for v in versiones if v["migracion"]],
            "versiones": versiones,
            "eventos_ordenados": eventos_ordenados,
        }
    )
    return salida


def a_json(escenario: dict) -> str:
    return json.dumps(escenario, indent=2, ensure_ascii=False) + "\n"


def _mmss(t):
    return f"{t // 60:3d}:{t % 60:02d}"


def linea_de_tiempo(escenario: dict) -> str:
    c = escenario["corrida"]
    lineas = [
        f"corrida c{c}  nivel {escenario['nivel']}  semilla {escenario['semilla']}  "
        f"generador {escenario['generador']['commit'][:7]} (formato {escenario['generador']['version_formato']})"
        + (f"  actualizaciones fijas {escenario['actualizaciones_fijas']}" if "actualizaciones_fijas" in escenario else "")
        + (f"  tipo fijo {escenario['tipo_fijo']}" if "tipo_fijo" in escenario else "")
        + (f"  brecha de actualización {escenario['brecha_actualizacion_seg']} s" if "brecha_actualizacion_seg" in escenario else "")
        + (f"  brecha del segundo repo {escenario['brecha_segundo_repo_seg']} s" if "brecha_segundo_repo_seg" in escenario else ""),
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
    parser.add_argument("--actualizaciones-fijas", type=int, choices=ACTUALIZACIONES_FIJAS, default=None,
                        help="cantidad fija de actualizaciones de cada versión (si no, se sortea)")
    parser.add_argument("--tipo-fijo", choices=TIPOS_FIJOS, default=None,
                        help="tipo de cambio de todas las versiones (si no, se sortea)")
    parser.add_argument("--brecha-actualizacion-seg", type=int, default=None,
                        help="brecha fija (s) de cada actualización desde el evento anterior de la cadena (si no, se sortea)")
    parser.add_argument("--brecha-segundo-repo-seg", type=int, default=None,
                        help="brecha fija (s) de la apertura del segundo repo de una combinación (si no, se sortea)")
    parser.add_argument("--salida", help="archivo JSON a escribir (por ejemplo escenarios/c1.json)")
    parser.add_argument("--ver", metavar="ARCHIVO", help="imprime la línea de tiempo de un escenario")
    args = parser.parse_args(argv)

    if args.ver:
        if any(x is not None for x in (args.corrida, args.nivel, args.semilla, args.salida, args.actualizaciones_fijas,
                                       args.tipo_fijo, args.brecha_actualizacion_seg, args.brecha_segundo_repo_seg)):
            parser.error("--ver no se combina con --corrida, --nivel, --semilla, --salida ni los parámetros fijos")
        print(linea_de_tiempo(json.loads(Path(args.ver).read_text(encoding="utf-8"))))
        return 0

    faltan = [f"--{k}" for k in ("corrida", "nivel", "semilla", "salida") if getattr(args, k) is None]
    if faltan:
        parser.error("faltan " + ", ".join(faltan) + " (o usar --ver ARCHIVO)")
    try:
        escenario = generar(args.corrida, args.nivel, args.semilla, args.actualizaciones_fijas, args.tipo_fijo,
                            args.brecha_actualizacion_seg, args.brecha_segundo_repo_seg)
    except ValueError as error:
        parser.error(str(error))
    salida = Path(args.salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(a_json(escenario), encoding="utf-8")
    fijas = "".join(f", {nombre} {valor}" for nombre, valor in (
        ("actualizaciones fijas", args.actualizaciones_fijas), ("tipo fijo", args.tipo_fijo),
        ("brecha de actualización", args.brecha_actualizacion_seg), ("brecha del segundo repo", args.brecha_segundo_repo_seg),
    ) if valor is not None)
    print(f"escenario c{args.corrida} (nivel {args.nivel}, semilla {args.semilla}{fijas}) en {salida}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
