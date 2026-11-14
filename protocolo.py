#!/usr/bin/env python3
"""Protocolo congelado del experimento: manifiesto versionado y verificación en vivo.

    python protocolo.py --generar protocolo/protocolo-v1.json   # relevamiento (solo lectura) y manifiesto
    python protocolo.py --verificar [protocolo/protocolo-v1.json]

El manifiesto registra los ETags de main/ del bucket de buildspecs, los commits de main de los repos, el
tag.json de main de los -cd, la imagen de la suite y PLAYWRIGHT_RETRIES, el commit de terraform fijado en los
proyectos efímeros (y el árbol de entorno/ en ese commit y en main), SIMULAR_TRABAJO_SEG, el commit de
tesis-generador y los parámetros del escenario, más la fecha y una huella (sha256 del JSON canónico sin el
campo huella).

La verificación (al inicio de cada corrida, antes del reset) compara en vivo: ETags de main/ (mismo conjunto
de objetos), tag.json de main de los -cd, imagen de la suite y PLAYWRIGHT_RETRIES en lo publicado, ninguna
variable de proyecto que pise SUITE_IMAGEN / PLAYWRIGHT_RETRIES / SIMULAR_TRABAJO_SEG, SIMULAR_TRABAJO_SEG en 0,
commit de terraform de los proyectos efímeros, HEAD de tesis-generador (el del manifiesto o un descendiente
que no toque escenario.py ni acciones.py, sin cambios locales en ellos) y parámetros del escenario.
"""

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import escenario as escenario_mod
import reset as reset_mod

RAIZ = Path(__file__).resolve().parent
MANIFIESTO = RAIZ / "protocolo" / "protocolo-v1.json"
REGION = "us-east-2"
ORG = "tesis-entornos"
BUCKET = "tesis-entornos-buildspecs-582621604637"
PREFIJO = "main/"
REPOS = ["tesis-backend", "tesis-frontend", "tesis-buildspecs", "tesis-terraform", "tesis-generador",
         "tesis-backend-cd", "tesis-frontend-cd"]
CDS = ["tesis-backend-cd", "tesis-frontend-cd"]
PROYECTOS_EFIMERO = ["tesis-efimero-desplegar-backend", "tesis-efimero-desplegar-frontend",
                     "tesis-efimero-cerrar-backend", "tesis-efimero-cerrar-frontend"]
VARIABLES_VIGILADAS = ("SUITE_IMAGEN", "PLAYWRIGHT_RETRIES", "SIMULAR_TRABAJO_SEG")
PROTEGIDOS = ["escenario.py", "acciones.py"]
TERRAFORM_SUBDIR = "entorno"


# --------------------------------------------------------------------------------------------------
# Huella


def canonico(d: dict) -> str:
    return json.dumps({k: v for k, v in d.items() if k != "huella"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def huella(d: dict) -> str:
    return hashlib.sha256(canonico(d).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------------
# Extracción de lo publicado (pura)


def suite_de(publicados: dict) -> dict:
    """publicados = {clave relativa a main/: texto}. Imagen de la suite (en los dos desplegar-*.yml),
    PLAYWRIGHT_RETRIES (ejecutar-suite.sh) y SIMULAR_TRABAJO_SEG (desplegar-efimero.yml)."""
    imagenes = {}
    for f in ("desplegar-efimero.yml", "desplegar-fijo.yml"):
        m = re.search(r"^\s*SUITE_IMAGEN:\s*(\S+)\s*$", publicados.get(f, ""), re.M)
        imagenes[f] = m.group(1) if m else None
    m = re.search(r"PLAYWRIGHT_RETRIES=(\d+)", publicados.get("scripts/ejecutar-suite.sh", ""))
    s = re.search(r"^\s*SIMULAR_TRABAJO_SEG:\s*\"?([^\"\s]*)\"?\s*$", publicados.get("desplegar-efimero.yml", ""), re.M)
    return {"imagenes": imagenes, "playwright_retries": int(m.group(1)) if m else None,
            "simular_trabajo_seg": s.group(1) if s else None}


# --------------------------------------------------------------------------------------------------
# Relevamiento en vivo (solo lectura)


def _correr(args, cwd=None):
    r = subprocess.run(args, cwd=cwd, text=True, capture_output=True, env=reset_mod._entorno())
    if r.returncode != 0:
        raise RuntimeError(f"falló {' '.join(args[:5])}: {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout


def _aws(perfil, *args):
    out = _correr(["aws", *args, "--profile", perfil, "--region", REGION, "--output", "json"])
    return json.loads(out) if out.strip() else None


def relevar_buildspecs():
    objetos = _aws("tf-compartida", "s3api", "list-objects-v2", "--bucket", BUCKET, "--prefix", PREFIJO,
                   "--query", "Contents[].[Key,ETag]") or []
    etags = {k[len(PREFIJO):]: e.strip('"') for k, e in objetos}
    publicados = {}
    for clave in ("desplegar-efimero.yml", "desplegar-fijo.yml", "scripts/ejecutar-suite.sh"):
        publicados[clave] = _correr(["aws", "s3", "cp", f"s3://{BUCKET}/{PREFIJO}{clave}", "-", "--only-show-errors",
                                     "--profile", "tf-compartida", "--region", REGION])
    return etags, suite_de(publicados)


def relevar_proyectos():
    """Variables de proyecto vigiladas (no deberían existir) y terraform fijado en los proyectos efímeros."""
    pisadas, terraform = [], {}
    for perfil, proyectos in reset_mod.PROYECTOS.items():
        for p in _aws(perfil, "codebuild", "batch-get-projects", "--names", *proyectos, "--query", "projects") or []:
            for v in (p.get("environment") or {}).get("environmentVariables") or []:
                if v["name"] in VARIABLES_VIGILADAS:
                    pisadas.append({"proyecto": p["name"], "variable": v["name"], "tipo": v["type"],
                                    "valor": v["value"] if v["type"] == "PLAINTEXT" else "<no PLAINTEXT>"})
            if p["name"] in PROYECTOS_EFIMERO:
                versiones = {s["sourceIdentifier"]: s["sourceVersion"] for s in p.get("secondarySourceVersions") or []}
                terraform[p["name"]] = versiones.get("terraform")
    return pisadas, terraform


def ref_main(repo):
    return _correr(["gh", "api", f"repos/{ORG}/{repo}/git/ref/heads/main", "-q", ".object.sha"]).strip()


def tag_json_main(cd):
    return json.loads(base64.b64decode(json.loads(_correr(["gh", "api", f"repos/{ORG}/{cd}/contents/tag.json?ref=main"]))["content"]))


def arbol(repo_dir, commit, subdir):
    return _correr(["git", "rev-parse", f"{commit}:{subdir}"], cwd=repo_dir).strip()


def parametros_escenario():
    return {"version_formato": escenario_mod.VERSION_FORMATO, "niveles": list(escenario_mod.NIVELES),
            "brecha": list(escenario_mod.BRECHA), "parametros": escenario_mod.PARAMETROS}


def relevar(terraform_dir=None):
    etags, suite = relevar_buildspecs()
    pisadas, terraform = relevar_proyectos()
    repos = {r: ref_main(r) for r in REPOS}
    fijados = sorted(set(terraform.values()))
    tf = {"main": repos["tesis-terraform"], "fijado": fijados[0] if len(fijados) == 1 else fijados,
          "proyectos": terraform, "subdir": TERRAFORM_SUBDIR}
    if terraform_dir and len(fijados) == 1:
        _correr(["git", "fetch", "--quiet", "origin"], cwd=terraform_dir)
        tf["arbol_subdir_fijado"] = arbol(terraform_dir, fijados[0], TERRAFORM_SUBDIR)
        tf["arbol_subdir_main"] = arbol(terraform_dir, repos["tesis-terraform"], TERRAFORM_SUBDIR)
        tf["fijado_es_ancestro_de_main"] = subprocess.run(
            ["git", "merge-base", "--is-ancestor", fijados[0], repos["tesis-terraform"]], cwd=terraform_dir).returncode == 0
    imagenes = set(suite["imagenes"].values())
    return {
        "nombre": "protocolo-v1",
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "buildspecs": {"bucket": BUCKET, "prefijo": PREFIJO, "etags": etags},
        "repos_main": repos,
        "tag_json_main": {cd: tag_json_main(cd) for cd in CDS},
        "suite": {"imagen": imagenes.pop() if len(imagenes) == 1 else sorted(imagenes),
                  "fuentes": sorted(suite["imagenes"]), "playwright_retries": suite["playwright_retries"],
                  "playwright_retries_fuente": "scripts/ejecutar-suite.sh"},
        "simular_trabajo_seg": {"valor_publicado": suite["simular_trabajo_seg"], "fuente": "desplegar-efimero.yml (env.variables)",
                                "variables_de_proyecto": pisadas},
        "terraform": tf,
        "generador": {"commit": repos["tesis-generador"], "protegidos": PROTEGIDOS},
        "escenario": parametros_escenario(),
    }


# --------------------------------------------------------------------------------------------------
# Verificación


def comparar(manifiesto: dict, vivo: dict) -> list:
    """Diferencias entre el manifiesto y lo relevado en vivo. vivo = {etags, suite: {imagenes,
    playwright_retries, simular_trabajo_seg}, pisadas, terraform: {proyecto: commit}, tag_json_main, escenario,
    escenario_archivo (parametros del escenario a correr, opcional), generador: [problemas]}."""
    d = []
    esperados, vistos = manifiesto["buildspecs"]["etags"], vivo["etags"]
    for k in sorted(set(esperados) | set(vistos)):
        if esperados.get(k) != vistos.get(k):
            d.append(f"ETag de {manifiesto['buildspecs']['prefijo']}{k}: manifiesto {esperados.get(k)}, publicado {vistos.get(k)}")
    for cd, tag in manifiesto["tag_json_main"].items():
        if vivo["tag_json_main"].get(cd) != tag:
            d.append(f"tag.json de main de {cd} distinto del manifiesto: {vivo['tag_json_main'].get(cd)}")
    for fuente, imagen in vivo["suite"]["imagenes"].items():
        if imagen != manifiesto["suite"]["imagen"]:
            d.append(f"imagen de la suite en {fuente}: {imagen}, manifiesto {manifiesto['suite']['imagen']}")
    if vivo["suite"]["playwright_retries"] != manifiesto["suite"]["playwright_retries"]:
        d.append(f"PLAYWRIGHT_RETRIES {vivo['suite']['playwright_retries']}, manifiesto {manifiesto['suite']['playwright_retries']}")
    if vivo["suite"]["simular_trabajo_seg"] not in ("0", ""):
        d.append(f"SIMULAR_TRABAJO_SEG publicado = {vivo['suite']['simular_trabajo_seg']!r} (tiene que ser 0)")
    for p in vivo["pisadas"]:
        d.append(f"el proyecto {p['proyecto']} define {p['variable']} ({p['valor']}), que pisa el buildspec")
    for proyecto, commit in vivo["terraform"].items():
        if commit != manifiesto["terraform"]["fijado"]:
            d.append(f"terraform de {proyecto}: {commit}, manifiesto {manifiesto['terraform']['fijado']}")
    if vivo["escenario"] != manifiesto["escenario"]:
        d.append("parámetros de escenario.py distintos de los del manifiesto")
    if vivo.get("escenario_archivo") is not None and vivo["escenario_archivo"] != manifiesto["escenario"]["parametros"]:
        d.append("los parámetros del escenario a correr no son los del manifiesto")
    d += vivo.get("generador", [])
    return d


def verificar_generador(commit_manifiesto: str, repo_dir=RAIZ) -> list:
    """HEAD = commit del manifiesto o un descendiente que no toque los protegidos, sin cambios locales en ellos."""
    def git(*a):
        return subprocess.run(["git", *a], cwd=repo_dir, text=True, capture_output=True)
    problemas = []
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != commit_manifiesto:
        if git("merge-base", "--is-ancestor", commit_manifiesto, "HEAD").returncode != 0:
            return [f"tesis-generador HEAD {head[:12]} no desciende del commit del manifiesto {commit_manifiesto[:12]}"]
        tocados = [x for x in git("diff", "--name-only", commit_manifiesto, "HEAD", "--", *PROTEGIDOS).stdout.split() if x]
        if tocados:
            problemas.append(f"tesis-generador: {', '.join(tocados)} cambió desde el commit del manifiesto")
    locales = [x for x in git("status", "--porcelain", "--", *PROTEGIDOS).stdout.splitlines() if x]
    if locales:
        problemas.append(f"tesis-generador: cambios sin commitear en {', '.join(PROTEGIDOS)}: {locales}")
    return problemas


def cargar(ruta=MANIFIESTO) -> dict:
    m = json.loads(Path(ruta).read_text(encoding="utf-8"))
    if m.get("huella") != huella(m):
        raise ValueError(f"la huella de {ruta} no coincide con su contenido")
    return m


def verificar_vivo(ruta=MANIFIESTO, escenario=None):
    """(manifiesto, diferencias). Solo lectura."""
    m = cargar(ruta)
    etags, suite = relevar_buildspecs()
    pisadas, terraform = relevar_proyectos()
    vivo = {"etags": etags, "suite": suite, "pisadas": pisadas, "terraform": terraform,
            "tag_json_main": {cd: tag_json_main(cd) for cd in m["tag_json_main"]},
            "escenario": parametros_escenario(),
            "escenario_archivo": None if escenario is None else escenario.get("parametros"),
            "generador": verificar_generador(m["generador"]["commit"])}
    return m, comparar(m, vivo)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manifiesto del protocolo y verificación en vivo")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--generar", metavar="ARCHIVO")
    g.add_argument("--verificar", metavar="ARCHIVO", nargs="?", const=str(MANIFIESTO))
    args = parser.parse_args(argv)
    if args.generar:
        m = relevar(terraform_dir=RAIZ.parent / "tesis-terraform")
        m["huella"] = huella(m)
        ruta = Path(args.generar)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"manifiesto {ruta} huella {m['huella']}")
        return 0
    m, diferencias = verificar_vivo(args.verificar)
    for x in diferencias:
        print(f"NO COINCIDE: {x}")
    print(f"protocolo {m['nombre']} huella {m['huella']}: {'ok' if not diferencias else f'{len(diferencias)} diferencias'}")
    return 0 if not diferencias else 1


if __name__ == "__main__":
    sys.exit(main())
