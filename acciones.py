#!/usr/bin/env python3
"""Acciones sobre las versiones sintéticas de un escenario: abrir y actualizar.

    python acciones.py abrir --escenario <f> --n <n> --repo backend|frontend [--dry-run]
    python acciones.py actualizar --escenario <f> --n <n> --repo <r> --k <k> [--dry-run]

abrir: crea la rama release/c<N>-<n> desde el commit de línea base del repo, escribe el contenido
sintético (marcador con actualizacion = 0 y, en backend, el catálogo de migraciones y el objetivo),
hace commit, push y abre el PR contra main.
actualizar: reescribe solo el marcador con actualizacion = k, hace commit y push (el PR se actualiza solo).

Con --dry-run todo pasa en el clon de trabajo local (rama, archivos, commit): no hay push ni PR, se muestra
el diff contra la línea base y el registro va a registros/dryrun-c<N>.jsonl (no versionado).
Solo usa la biblioteca estándar más git y gh por subprocess.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
TRABAJO = RAIZ / ".trabajo"
REGISTROS = RAIZ / "registros"

REPOS = ("backend", "frontend")
LINEA_BASE = {
    "backend": "afc16b1d85fc0e87e0998d2e69a2109520ed9719",
    "frontend": "dd2a13341c32c57275692bc6e43ef446fe4306d2",
}
REMOTOS = {
    "backend": "https://github.com/tesis-entornos/tesis-backend.git",
    "frontend": "https://github.com/tesis-entornos/tesis-frontend.git",
}
MARCADOR = {"backend": "app/sintetico.json", "frontend": "public/sintetico.json"}
VERSIONES_ALEMBIC = "app/alembic/versions"
OBJETIVO = "app/alembic/objetivo"
H0 = "e3acbde9bd2f"  # última migración de la línea base

IDENTIDAD = ("martincho", "mcometta211@alumnos.iua.edu.ar")


class ErrorAccion(Exception):
    pass


# --------------------------------------------------------------------------------------------------
# Contenido sintético (funciones puras)


def contenido_marcador(corrida: int, n: int, k: int) -> str:
    return json.dumps({"version": f"c{corrida}-{n}", "actualizacion": k}, indent=2) + "\n"


def contenido_migracion(revision: str, propia: bool) -> str:
    if propia:
        tabla = f"tesis_{revision}"
        upgrade = (
            f"    op.create_table(\n"
            f"        '{tabla}',\n"
            f"        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),\n"
            f"        sa.Column('nota', sa.Text(), nullable=True),\n"
            f"        sa.PrimaryKeyConstraint('id'),\n"
            f"    )\n"
        )
        downgrade = f"    op.drop_table('{tabla}')\n"
        descripcion = f"Migración sintética {revision}: crea la tabla {tabla}"
    else:
        upgrade = downgrade = "    pass\n"
        descripcion = f"Migración sintética {revision} (vacía en esta rama)"
    return (
        f'"""{descripcion}\n'
        f"\n"
        f"Revision ID: {revision}\n"
        f"Revises: {H0}\n"
        f'"""\n'
        f"from alembic import op\n"
        f"import sqlalchemy as sa\n"
        f"\n"
        f"\n"
        f"# revision identifiers, used by Alembic.\n"
        f"revision = {revision!r}\n"
        f"down_revision = {H0!r}\n"
        f"branch_labels = None\n"
        f"depends_on = None\n"
        f"\n"
        f"\n"
        f"def upgrade():\n"
        f"{upgrade}"
        f"\n"
        f"\n"
        f"def downgrade():\n"
        f"{downgrade}"
    )


def version(escenario: dict, n: int) -> dict:
    for v in escenario["versiones"]:
        if v["n"] == n:
            return v
    raise ErrorAccion(f"el escenario c{escenario['corrida']} no tiene la versión n = {n}")


def archivos_apertura(escenario: dict, n: int, repo: str) -> dict:
    """Ruta relativa -> contenido de todo lo que escribe la apertura de (n, repo)."""
    corrida = escenario["corrida"]
    v = version(escenario, n)
    archivos = {MARCADOR[repo]: contenido_marcador(corrida, n, 0)}
    if repo == "backend":
        propia = v["migracion"]
        for revision in escenario["catalogo_migraciones"]:
            archivos[f"{VERSIONES_ALEMBIC}/{revision}.py"] = contenido_migracion(revision, revision == propia)
        archivos[OBJETIVO] = f"{propia or H0}\n"
    return archivos


def archivos_actualizacion(escenario: dict, n: int, repo: str, k: int) -> dict:
    return {MARCADOR[repo]: contenido_marcador(escenario["corrida"], n, k)}


# --------------------------------------------------------------------------------------------------
# Validación contra el escenario


def validar_apertura(escenario: dict, n: int, repo: str) -> dict:
    v = version(escenario, n)
    if repo not in v["repos"]:
        raise ErrorAccion(f"{v['rama']} ({v['tipo']}) no toca {repo}; repos: {', '.join(v['repos'])}")
    return v


def validar_actualizacion(escenario: dict, n: int, repo: str, k: int, k_actual: int) -> dict:
    v = validar_apertura(escenario, n, repo)
    if k != k_actual + 1:
        raise ErrorAccion(f"{v['rama']} {repo}: el marcador está en {k_actual}; la siguiente es {k_actual + 1}, no {k}")
    if not any(e["repo"] == repo and e["accion"] == "actualizacion" and e["actualizacion"] == k for e in v["eventos"]):
        raise ErrorAccion(f"el escenario no tiene la actualización {k} de {v['rama']} en {repo}")
    return v


# --------------------------------------------------------------------------------------------------
# git / gh


def _ejecutar(args, cwd=None, capturar=True):
    r = subprocess.run(args, cwd=cwd, text=True, capture_output=capturar)
    if r.returncode != 0:
        raise ErrorAccion(f"falló {' '.join(args)}:\n{(r.stderr or '') + (r.stdout or '')}".rstrip())
    return r.stdout.strip() if capturar else ""


def _git(clon, *args):
    return _ejecutar(["git", *args], cwd=clon)


def preparar_clon(repo: str, trabajo: Path, remoto: str) -> Path:
    clon = trabajo / repo
    if not (clon / ".git").exists():
        trabajo.mkdir(parents=True, exist_ok=True)
        _ejecutar(["git", "clone", "--quiet", remoto, str(clon)])
    _git(clon, "config", "--local", "user.name", IDENTIDAD[0])
    _git(clon, "config", "--local", "user.email", IDENTIDAD[1])
    _git(clon, "fetch", "--quiet", "--prune", "origin")
    return clon


def _escribir(clon: Path, archivos: dict):
    for ruta, contenido in archivos.items():
        destino = clon / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(contenido, encoding="utf-8")
    _git(clon, "add", "--", *archivos)


def _existe(clon: Path, ref: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=clon, capture_output=True).returncode == 0


def _diff(clon: Path, base: str) -> str:
    r = subprocess.run(["git", "diff", "--stat", "-p", f"{base}..HEAD"], cwd=clon, text=True, capture_output=True)
    if r.returncode != 0:
        raise ErrorAccion(f"falló git diff:\n{r.stderr}".rstrip())
    return r.stdout.rstrip("\n")


def _registrar(escenario, accion, n, repo, rama, k, commit, pr, pr_created_at, dry_run, registros: Path):
    corrida = escenario["corrida"]
    registros.mkdir(parents=True, exist_ok=True)
    archivo = registros / (f"dryrun-c{corrida}.jsonl" if dry_run else f"c{corrida}.jsonl")
    linea = {
        "t_real": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "accion": accion,
        "n": n,
        "repo": repo,
        "rama": rama,
        "k": k,
        "commit": commit,
        "pr": pr,
        "pr_created_at": pr_created_at,
        "dry_run": dry_run,
    }
    with archivo.open("a", encoding="utf-8") as f:
        f.write(json.dumps(linea, ensure_ascii=False) + "\n")
    return linea


def _cargar(escenario):
    if isinstance(escenario, dict):
        return escenario
    return json.loads(Path(escenario).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------------
# Acciones


def abrir(escenario, n: int, repo: str, dry_run: bool, *, trabajo=TRABAJO, registros=REGISTROS, remoto=None, base=None):
    escenario = _cargar(escenario)
    v = validar_apertura(escenario, n, repo)
    corrida, rama = escenario["corrida"], v["rama"]
    base = base or LINEA_BASE[repo]
    clon = preparar_clon(repo, Path(trabajo), remoto or REMOTOS[repo])

    if not dry_run and _existe(clon, f"refs/remotes/origin/{rama}"):
        raise ErrorAccion(f"{rama} ya existe en el remoto de {repo}")
    _git(clon, "checkout", "--quiet", "-B", rama, base)
    _escribir(clon, archivos_apertura(escenario, n, repo))
    _git(clon, "commit", "--quiet", "-m", f"c{corrida}-{n} {repo} apertura")
    commit = _git(clon, "rev-parse", "HEAD")

    pr = pr_created_at = None
    diff = None
    if dry_run:
        diff = _diff(clon, base)
    else:
        _git(clon, "push", "--quiet", "-u", "origin", rama)
        _ejecutar(["gh", "pr", "create", "--base", "main", "--head", rama, "--title", rama,
                   "--body", f"generador c{corrida}"], cwd=clon)
        datos = json.loads(_ejecutar(["gh", "pr", "view", rama, "--json", "number,createdAt"], cwd=clon))
        pr, pr_created_at = datos["number"], datos["createdAt"]

    registro = _registrar(escenario, "apertura", n, repo, rama, 0, commit, pr, pr_created_at, dry_run, Path(registros))
    return registro, diff


def actualizar(escenario, n: int, repo: str, k: int, dry_run: bool, *, trabajo=TRABAJO, registros=REGISTROS, remoto=None, base=None):
    escenario = _cargar(escenario)
    v = validar_apertura(escenario, n, repo)
    corrida, rama = escenario["corrida"], v["rama"]
    base = base or LINEA_BASE[repo]
    clon = preparar_clon(repo, Path(trabajo), remoto or REMOTOS[repo])

    # Rama existente: la del remoto si hay (modo real); la local del dry-run si no.
    if dry_run:
        if not _existe(clon, f"refs/heads/{rama}"):
            raise ErrorAccion(f"no existe la rama local {rama} en {repo}: primero abrir (con --dry-run)")
        _git(clon, "checkout", "--quiet", rama)
    else:
        if not _existe(clon, f"refs/remotes/origin/{rama}"):
            raise ErrorAccion(f"{rama} no está en el remoto de {repo}: primero abrir")
        _git(clon, "checkout", "--quiet", "-B", rama, f"origin/{rama}")

    marcador = clon / MARCADOR[repo]
    try:
        actual = json.loads(marcador.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ErrorAccion(f"no se pudo leer {MARCADOR[repo]} en {rama}: {error}")
    if actual.get("version") != f"c{corrida}-{n}":
        raise ErrorAccion(f"el marcador de {rama} es de {actual.get('version')}, no de c{corrida}-{n}")
    validar_actualizacion(escenario, n, repo, k, actual["actualizacion"])

    _escribir(clon, archivos_actualizacion(escenario, n, repo, k))
    _git(clon, "commit", "--quiet", "-m", f"c{corrida}-{n} {repo} actualizacion {k}")
    commit = _git(clon, "rev-parse", "HEAD")

    diff = None
    if dry_run:
        diff = _diff(clon, base)
    else:
        _git(clon, "push", "--quiet", "origin", rama)

    registro = _registrar(escenario, "actualizacion", n, repo, rama, k, commit, None, None, dry_run, Path(registros))
    return registro, diff


def main(argv=None):
    parser = argparse.ArgumentParser(description="Acciones sobre las versiones sintéticas de un escenario")
    sub = parser.add_subparsers(dest="accion", required=True)
    for nombre in ("abrir", "actualizar"):
        p = sub.add_parser(nombre)
        p.add_argument("--escenario", required=True)
        p.add_argument("--n", type=int, required=True)
        p.add_argument("--repo", choices=REPOS, required=True)
        if nombre == "actualizar":
            p.add_argument("--k", type=int, required=True)
        p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.accion == "abrir":
            registro, diff = abrir(args.escenario, args.n, args.repo, args.dry_run)
        else:
            registro, diff = actualizar(args.escenario, args.n, args.repo, args.k, args.dry_run)
    except ErrorAccion as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if diff is not None:
        print(diff)
    print(json.dumps(registro, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
