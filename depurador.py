#!/usr/bin/env python3
"""Depurador de fin de corrida: busca restos y los limpia.

    python depurador.py [--solo-buscar]

Restos que busca: servicios ACTIVE en efimero-cluster; disponible.json en el bucket de states del efímero
(solo claves); reglas no default en el listener de efimero-alb; distribuciones CloudFront con tag corrida;
ramas release/* en los cuatro repos; PR abiertos con head release/*; builds IN_PROGRESS en los ocho
proyectos. Las task definitions INACTIVE no son restos.

Acción, en este orden: esperar a que no haya builds en curso; por cada entorno efímero vivo (servicio,
disponible.json o distribución), cierre manual (start-build del proyecto de cierre con RAMA) y esperar la
destrucción; cerrar sin mergear los PR abiertos; esperar los builds que eso dispare; borrar las ramas;
esperar otra vez y volver a buscar. Devuelve lo encontrado (antes de actuar), lo hecho y lo que quedó.
"""

import argparse
import json
import re
import subprocess
import sys
import time

import reset as reset_mod

REGION = "us-east-2"
ORG = "tesis-entornos"
REPOS = ["tesis-backend", "tesis-frontend", "tesis-backend-cd", "tesis-frontend-cd"]
EFIMERO_CLUSTER = "efimero-cluster"
EFIMERO_ALB = "efimero-alb"
EFIMERO_STATES = "tesis-entornos-tfstate-398846004421"
REGISTROS_EFIMERO = "tesis-efimero-registros-398846004421"
PROYECTO_CIERRE = "tesis-efimero-cerrar-backend"
SONDEO_SEG = 30
MAX_BUILDS_SEG = 60 * 60
MAX_CIERRE_SEG = 30 * 60


class ErrorDepurador(Exception):
    pass


# --------------------------------------------------------------------------------------------------
# Funciones puras


def rama_de_slug(slug: str):
    m = re.match(r"^(release|hotfix)-(c\d+-\d+)$", slug or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def slug_de_servicio(nombre: str):
    m = re.match(r"^efimero-(.+)-(backend|frontend)$", nombre or "")
    return m.group(1) if m else None


def entornos_vivos(restos: list) -> list:
    """Ramas con un entorno efímero vivo (servicio, disponible.json o distribución), sin repetir."""
    ramas = []
    for r in restos:
        if r["tipo"] in ("servicio", "disponible", "cloudfront") and r.get("rama") and r["rama"] not in ramas:
            ramas.append(r["rama"])
    return ramas


def plan(restos: list) -> list:
    """Pasos a ejecutar para limpiar los restos, en orden."""
    pasos = [("esperar_builds",)]
    pasos += [("cierre_manual", rama) for rama in entornos_vivos(restos)]
    prs = [r for r in restos if r["tipo"] == "pr"]
    pasos += [("cerrar_pr", r["repo"], r["numero"]) for r in prs]
    if prs:
        pasos.append(("esperar_builds",))
    pasos += [("borrar_rama", r["repo"], r["rama"]) for r in restos if r["tipo"] == "rama"]
    if any(p[0] in ("cierre_manual", "borrar_rama") for p in pasos):
        pasos.append(("esperar_builds",))
    return pasos


# --------------------------------------------------------------------------------------------------
# Consultas y acciones reales


def _correr(args, check=True):
    r = subprocess.run(args, text=True, capture_output=True, env=reset_mod._entorno())
    if check and r.returncode != 0:
        raise ErrorDepurador(f"falló {' '.join(args[:5])}: {reset_mod.redactar((r.stderr or r.stdout).strip())[:500]}")
    return r


def _aws(perfil, *args):
    out = _correr(["aws", *args, "--profile", perfil, "--region", REGION, "--output", "json"]).stdout
    return json.loads(out) if out.strip() else None


class Real:
    def builds_en_curso(self):
        en_curso = []
        for perfil, proyectos in reset_mod.PROYECTOS.items():
            for p in proyectos:
                ids = _aws(perfil, "codebuild", "list-builds-for-project", "--project-name", p, "--query", "ids[0:10]") or []
                if ids:
                    en_curso += _aws(perfil, "codebuild", "batch-get-builds", "--ids", *ids,
                                     "--query", "builds[?buildStatus=='IN_PROGRESS'].id") or []
        return en_curso

    def buscar(self):
        restos = []
        servicios = _aws("tf-efimero", "ecs", "list-services", "--cluster", EFIMERO_CLUSTER, "--query", "serviceArns") or []
        for i in range(0, len(servicios), 10):
            for nombre in _aws("tf-efimero", "ecs", "describe-services", "--cluster", EFIMERO_CLUSTER, "--services",
                               *servicios[i:i + 10], "--query", "services[?status=='ACTIVE'].serviceName") or []:
                restos.append({"tipo": "servicio", "nombre": nombre, "rama": rama_de_slug(slug_de_servicio(nombre))})
        claves = _aws("tf-efimero", "s3api", "list-objects-v2", "--bucket", EFIMERO_STATES, "--prefix", "entornos/",
                      "--query", "Contents[].Key") or []
        for c in claves:
            if c.endswith("/disponible.json"):
                restos.append({"tipo": "disponible", "clave": c, "rama": rama_de_slug(c.split("/")[1])})
        lb = _aws("tf-efimero", "elbv2", "describe-load-balancers", "--names", EFIMERO_ALB, "--query", "LoadBalancers[0].LoadBalancerArn")
        for listener in _aws("tf-efimero", "elbv2", "describe-listeners", "--load-balancer-arn", lb, "--query", "Listeners[].ListenerArn") or []:
            for regla in _aws("tf-efimero", "elbv2", "describe-rules", "--listener-arn", listener, "--query", "Rules") or []:
                if not regla.get("IsDefault"):
                    hosts = [v for c in regla.get("Conditions", []) for v in (c.get("Values") or (c.get("HostHeaderConfig") or {}).get("Values", []))]
                    restos.append({"tipo": "regla_alb", "prioridad": regla.get("Priority"), "hosts": hosts})
        for arn in _aws("tf-efimero", "cloudfront", "list-distributions", "--query", "DistributionList.Items[].ARN") or []:
            tags = {t["Key"]: t["Value"] for t in (_aws("tf-efimero", "cloudfront", "list-tags-for-resource", "--resource", arn,
                                                         "--query", "Tags.Items") or [])}
            if "corrida" in tags:
                restos.append({"tipo": "cloudfront", "arn": arn, "corrida": tags["corrida"], "rama": rama_de_slug(tags.get("entorno"))})
        for repo in REPOS:
            out = _correr(["git", "ls-remote", "--heads", f"https://github.com/{ORG}/{repo}.git", "release/*"]).stdout
            for linea in out.splitlines():
                restos.append({"tipo": "rama", "repo": repo, "rama": linea.split("refs/heads/", 1)[1]})
            prs = json.loads(_correr(["gh", "pr", "list", "-R", f"{ORG}/{repo}", "--state", "open", "--limit", "200",
                                      "--json", "number,headRefName,baseRefName"]).stdout)
            for p in prs:
                if p["headRefName"].startswith("release/"):
                    restos.append({"tipo": "pr", "repo": repo, "numero": p["number"], "head": p["headRefName"], "base": p["baseRefName"]})
        restos += [{"tipo": "build", "id": b} for b in self.builds_en_curso()]
        return restos

    def esperar_builds(self, log):
        t0 = time.time()
        while True:
            en_curso = self.builds_en_curso()
            if not en_curso:
                return
            if time.time() - t0 > MAX_BUILDS_SEG:
                raise ErrorDepurador(f"builds en curso después de {MAX_BUILDS_SEG // 60} min: {en_curso}")
            log(f"esperando builds en curso: {', '.join(en_curso)}")
            time.sleep(SONDEO_SEG)

    def cierre_manual(self, rama, log):
        bid = _aws("tf-efimero", "codebuild", "start-build", "--project-name", PROYECTO_CIERRE,
                   "--environment-variables-override", f"name=RAMA,value={rama},type=PLAINTEXT", "--query", "build.id")
        log(f"cierre manual de {rama}: {bid}")
        t0 = time.time()
        while True:
            b = _aws("tf-efimero", "codebuild", "batch-get-builds", "--ids", bid, "--query", "builds[0]")
            if b["buildComplete"]:
                break
            if time.time() - t0 > MAX_CIERRE_SEG:
                raise ErrorDepurador(f"el cierre manual {bid} no terminó en {MAX_CIERRE_SEG // 60} min")
            time.sleep(SONDEO_SEG)
        corrida = re.search(r"/(c\d+)-", rama).group(1)
        r = _correr(["aws", "s3", "cp", f"s3://{REGISTROS_EFIMERO}/destrucciones/corrida={corrida}/{bid.split(':', 1)[1]}/destruccion.json",
                     "-", "--only-show-errors", "--profile", "tf-efimero", "--region", REGION], check=False)
        d = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
        return {"build_id": bid, "status": b["buildStatus"], "resultado": d.get("resultado"),
                "accion": (d.get("cierre") or {}).get("accion")}

    def cerrar_pr(self, repo, numero):
        _correr(["gh", "pr", "close", str(numero), "-R", f"{ORG}/{repo}"])

    def borrar_rama(self, repo, rama):
        r = _correr(["gh", "api", "-X", "DELETE", f"repos/{ORG}/{repo}/git/refs/heads/{rama}"], check=False)
        return r.returncode == 0


# --------------------------------------------------------------------------------------------------


def ahora():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def depurar(consultas=None, solo_buscar=False, log=print):
    c = consultas or Real()
    informe = {"inicio": ahora(), "encontrados": c.buscar(), "acciones": [], "restantes": None, "fin": None}
    log(f"depurador: {len(informe['encontrados'])} restos encontrados")
    for r in informe["encontrados"]:
        log(f"  resto {json.dumps(r, ensure_ascii=False)}")
    if solo_buscar or not informe["encontrados"]:
        informe.update(restantes=informe["encontrados"], fin=ahora())
        return informe
    for paso in plan(informe["encontrados"]):
        hecho = {"paso": paso[0], "args": list(paso[1:]), "hora": ahora()}
        if paso[0] == "esperar_builds":
            c.esperar_builds(log)
        elif paso[0] == "cierre_manual":
            hecho["resultado"] = c.cierre_manual(paso[1], log)
        elif paso[0] == "cerrar_pr":
            c.cerrar_pr(paso[1], paso[2])
        elif paso[0] == "borrar_rama":
            hecho["resultado"] = c.borrar_rama(paso[1], paso[2])
        informe["acciones"].append(hecho)
        log(f"  hecho {paso[0]} {' '.join(map(str, paso[1:]))} {hecho.get('resultado', '')}")
    informe["restantes"] = c.buscar()
    informe["fin"] = ahora()
    log(f"depurador: {len(informe['restantes'])} restos después de actuar")
    return informe


def main(argv=None):
    parser = argparse.ArgumentParser(description="Busca y limpia restos de una corrida")
    parser.add_argument("--solo-buscar", action="store_true")
    args = parser.parse_args(argv)
    informe = depurar(solo_buscar=args.solo_buscar)
    print(json.dumps(informe, indent=2, ensure_ascii=False))
    return 0 if not informe["restantes"] else 1


if __name__ == "__main__":
    sys.exit(main())
