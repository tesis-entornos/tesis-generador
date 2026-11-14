#!/usr/bin/env python3
"""Reset de inicio de corrida: deja el entorno compartido del fijo en la línea base.

    python reset.py --corrida N [--dry-run]

Pasos (si uno falla, se detiene ahí y deja el registro con lo hecho):
  1. precondiciones: ningún build IN_PROGRESS en los ocho proyectos, efímero vacío (sin servicios ACTIVE ni
     disponible.json) y ningún PR abierto con head release/* en los cuatro repos;
  2. primer reset de datos del fijo: tarea one-off con scripts/reset_baseline.py sobre la task definition
     vigente del servicio (o una copia con la imagen de la línea base si la vigente tiene otra);
  3. despliegue de la línea base en dev de tesis-backend-cd y después de tesis-frontend-cd (tag.json = el de
     main, commit aunque no haya cambios), esperando cada build del fijo con build_ok y suite ok;
  4. segundo reset de datos, con la task definition que dejó el despliegue (imagen de la línea base);
  5. main-fijo y main-efimero de los dos -cd al SHA de main (la única actualización forzada permitida);
  6. registro en registros/reset-c<N>.json. La hora de fin del reset es el t = 0 de la corrida.

Con --dry-run solo consulta (AWS, GitHub, git fetch en los clones de trabajo) y muestra lo que haría.
Biblioteca estándar más aws, git y gh por subprocess.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import acciones

RAIZ = Path(__file__).resolve().parent
REGISTROS = RAIZ / "registros"
TRABAJO = RAIZ / ".trabajo"

REGION = "us-east-2"
PERFIL_FIJO, PERFIL_EFIMERO, PERFIL_COMPARTIDA = "tf-fijo", "tf-efimero", "tf-compartida"
ORG = "tesis-entornos"

PROYECTOS = {
    PERFIL_COMPARTIDA: ["tesis-build-backend", "tesis-build-frontend"],
    PERFIL_EFIMERO: [
        "tesis-efimero-desplegar-backend",
        "tesis-efimero-desplegar-frontend",
        "tesis-efimero-cerrar-backend",
        "tesis-efimero-cerrar-frontend",
    ],
    PERFIL_FIJO: ["tesis-fijo-desplegar-backend", "tesis-fijo-desplegar-frontend"],
}
REPOS_PR = ["tesis-backend", "tesis-frontend", "tesis-backend-cd", "tesis-frontend-cd"]
CDS = [("tesis-backend-cd", "tesis-fijo-desplegar-backend"), ("tesis-frontend-cd", "tesis-fijo-desplegar-frontend")]
RAMAS_CIERRE = ["main-fijo", "main-efimero"]

EFIMERO_CLUSTER = "efimero-cluster"
EFIMERO_STATES = "tesis-entornos-tfstate-398846004421"

FIJO_CLUSTER = "fijo-cluster"
FIJO_SERVICIO = "fijo-compartido-backend"
FIJO_CONTENEDOR = "backend"
FIJO_BASE = "app_compartido"
FIJO_LOGS = "/ecs/fijo"
FIJO_STREAM = "compartido/backend/{task}"
RESET_COMANDO = ["python", "scripts/reset_baseline.py"]

# Campos registrables de una task definition que se copian tal cual.
CAMPOS_TD = [
    "family", "taskRoleArn", "executionRoleArn", "networkMode", "cpu", "memory", "runtimePlatform",
    "requiresCompatibilities", "containerDefinitions", "volumes",
]

SONDEO_SEG = 30
MAX_BUILD_SEG = 40 * 60
LINEAS_LOG = 40


class ErrorReset(Exception):
    pass


def ahora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(msg):
    print(f"[{ahora()}] {msg}", flush=True)


# --------------------------------------------------------------------------------------------------
# Funciones puras


def validar_precondiciones(datos: dict) -> list:
    """datos = {builds: {proyecto: [estado...]}, servicios_efimero: [arn...], disponibles: [clave...],
    prs: {repo: [head...]}}. Devuelve la lista de fallos (vacía si se puede seguir)."""
    fallos = []
    for proyecto, estados in datos["builds"].items():
        if "IN_PROGRESS" in estados:
            fallos.append(f"build IN_PROGRESS en {proyecto}")
    if datos["servicios_efimero"]:
        fallos.append(f"{EFIMERO_CLUSTER} tiene servicios ACTIVE: {', '.join(datos['servicios_efimero'])}")
    if datos["disponibles"]:
        fallos.append(f"hay disponible.json en el efímero: {', '.join(datos['disponibles'])}")
    for repo, heads in datos["prs"].items():
        release = [h for h in heads if h.startswith("release/")]
        if release:
            fallos.append(f"PR abierto con head release/* en {repo}: {', '.join(release)}")
    return fallos


def imagen_contenedor(td: dict, contenedor: str = FIJO_CONTENEDOR) -> str:
    for c in td["containerDefinitions"]:
        if c["name"] == contenedor:
            return c["image"]
    raise ErrorReset(f"la task definition {td.get('taskDefinitionArn')} no tiene el contenedor {contenedor}")


def copia_task_definition(td: dict, imagen: str, contenedor: str = FIJO_CONTENEDOR) -> dict:
    """Entrada para register-task-definition: los mismos campos registrables, con otra imagen en el contenedor."""
    copia = {campo: json.loads(json.dumps(td[campo])) for campo in CAMPOS_TD if campo in td}
    encontrado = False
    for c in copia["containerDefinitions"]:
        if c["name"] == contenedor:
            c["image"] = imagen
            encontrado = True
    if not encontrado:
        raise ErrorReset(f"la task definition no tiene el contenedor {contenedor}")
    return copia


def overrides_reset(base: str = FIJO_BASE) -> dict:
    return {
        "containerOverrides": [
            {"name": FIJO_CONTENEDOR, "command": RESET_COMANDO, "environment": [{"name": "RESET_CONFIRM", "value": base}]}
        ]
    }


_CREDENCIAL_URL = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")


def redactar(linea: str) -> str:
    """Tapa usuario:contraseña en URLs (por si un error imprime un DSN)."""
    return _CREDENCIAL_URL.sub(r"\1***:***@", linea)


def extraer_reporte(lineas: list):
    """Último objeto JSON multilínea del log de reset_baseline.py (empieza con '{' y termina con '}')."""
    fin = None
    for i in range(len(lineas) - 1, -1, -1):
        if lineas[i].rstrip() == "}":
            fin = i
            break
    if fin is None:
        return None
    for i in range(fin, -1, -1):
        if lineas[i].rstrip() == "{":
            try:
                return json.loads("\n".join(lineas[i : fin + 1]))
            except ValueError:
                return None
    return None


def elegir_build(builds: list, commit: str, previos: set):
    """El build nuevo (id fuera de previos) cuyo commit resuelto o sourceVersion es el commit empujado."""
    for b in builds:
        if b["id"] in previos:
            continue
        if commit in (b.get("resolvedSourceVersion"), b.get("sourceVersion")):
            return b
    return None


def accion_rama(sha_actual, sha_main: str) -> str:
    if sha_actual is None:
        return "crear"
    return "nada" if sha_actual == sha_main else "actualizar"


def validar_validacion(v: dict) -> list:
    fallos = []
    if v.get("build_ok") is not True:
        fallos.append(f"build_ok = {v.get('build_ok')}")
    suite = v.get("suite") or {}
    if suite.get("resultado") != "ok":
        fallos.append(f"suite = {suite.get('resultado')} ({v.get('suite_motivo')})")
    return fallos


# --------------------------------------------------------------------------------------------------
# aws / gh / git


def _entorno():
    return {k: v for k, v in os.environ.items()
            if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE")}


def _correr(args, cwd=None, entrada=None):
    r = subprocess.run(args, cwd=cwd, text=True, capture_output=True, env=_entorno(), input=entrada)
    if r.returncode != 0:
        raise ErrorReset(f"falló {' '.join(args[:4])} ...: {redactar((r.stderr or r.stdout).strip())[:800]}")
    return r.stdout


def aws(perfil, *args, salida_json=True):
    out = _correr(["aws", *args, "--profile", perfil, "--region", REGION, *(["--output", "json"] if salida_json else [])])
    return json.loads(out) if salida_json and out.strip() else out


def gh_api(ruta, *args):
    out = _correr(["gh", "api", ruta, *args])
    return json.loads(out) if out.strip() else None


def ref_sha(repo, rama):
    r = subprocess.run(["gh", "api", f"repos/{ORG}/{repo}/git/ref/heads/{rama}"], text=True, capture_output=True)
    if r.returncode != 0:
        if "Not Found" in (r.stdout + r.stderr):
            return None
        raise ErrorReset(f"no se pudo leer {repo} {rama}: {(r.stderr or r.stdout).strip()[:300]}")
    return json.loads(r.stdout)["object"]["sha"]


def tag_json_main(repo):
    datos = gh_api(f"repos/{ORG}/{repo}/contents/tag.json?ref=main")
    return json.loads(base64.b64decode(datos["content"]))


# --------------------------------------------------------------------------------------------------
# Pasos


def recoger_precondiciones():
    builds = {}
    for perfil, proyectos in PROYECTOS.items():
        for p in proyectos:
            ids = aws(perfil, "codebuild", "list-builds-for-project", "--project-name", p, "--query", "ids[0:10]") or []
            estados = aws(perfil, "codebuild", "batch-get-builds", "--ids", *ids, "--query", "builds[].buildStatus") if ids else []
            builds[p] = estados
    servicios = aws(PERFIL_EFIMERO, "ecs", "list-services", "--cluster", EFIMERO_CLUSTER, "--query", "serviceArns") or []
    activos = []
    if servicios:
        for i in range(0, len(servicios), 10):
            d = aws(PERFIL_EFIMERO, "ecs", "describe-services", "--cluster", EFIMERO_CLUSTER, "--services", *servicios[i:i + 10],
                    "--query", "services[?status=='ACTIVE'].serviceName")
            activos += d or []
    # Solo claves (nunca el contenido de los states).
    claves = aws(PERFIL_EFIMERO, "s3api", "list-objects-v2", "--bucket", EFIMERO_STATES, "--prefix", "entornos/",
                 "--query", "Contents[].Key") or []
    disponibles = [c for c in claves if c.endswith("/disponible.json")]
    prs = {}
    for repo in REPOS_PR:
        lista = json.loads(_correr(["gh", "pr", "list", "-R", f"{ORG}/{repo}", "--state", "open", "--limit", "200",
                                    "--json", "headRefName"]))
        prs[repo] = [p["headRefName"] for p in lista]
    return {"builds": builds, "servicios_efimero": activos, "disponibles": disponibles, "prs": prs}


def servicio_fijo():
    s = aws(PERFIL_FIJO, "ecs", "describe-services", "--cluster", FIJO_CLUSTER, "--services", FIJO_SERVICIO,
            "--query", "services[0]")
    if not s or s.get("status") != "ACTIVE":
        raise ErrorReset(f"el servicio {FIJO_SERVICIO} no está ACTIVE")
    return s


def describir_td(arn):
    return aws(PERFIL_FIJO, "ecs", "describe-task-definition", "--task-definition", arn, "--query", "taskDefinition")


def _logs_tarea(task_id):
    stream = FIJO_STREAM.format(task=task_id)
    lineas, token = [], None
    while True:
        args = ["logs", "get-log-events", "--log-group-name", FIJO_LOGS, "--log-stream-name", stream, "--start-from-head"]
        if token:
            args += ["--next-token", token]
        r = aws(PERFIL_FIJO, *args)
        lineas += [e["message"].rstrip("\n") for e in r["events"]]
        if r["nextForwardToken"] == token:
            break
        token = r["nextForwardToken"]
    return lineas


def reset_datos(paso, corrida, imagen_base, dry_run, permitir_copia):
    paso["inicio"] = ahora()
    s = servicio_fijo()
    td_arn = s["taskDefinition"]
    red = s["networkConfiguration"]
    td = describir_td(td_arn)
    imagen = imagen_contenedor(td)
    paso.update(task_definition_servicio=td_arn, imagen_servicio=imagen, red=red)
    copia_arn = None
    usar = td_arn
    if imagen != imagen_base:
        if not permitir_copia:
            raise ErrorReset(f"la task definition {td_arn} tiene {imagen}, no la línea base {imagen_base}")
        paso["copia"] = True
        if dry_run:
            log(f"  haría una copia de {td_arn} con la imagen {imagen_base}")
            usar = "<copia>"
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(copia_task_definition(td, imagen_base), f)
                ruta = f.name
            try:
                copia_arn = aws(PERFIL_FIJO, "ecs", "register-task-definition", "--cli-input-json", f"file://{ruta}",
                                "--query", "taskDefinition.taskDefinitionArn")
            finally:
                os.unlink(ruta)
            usar = copia_arn
            log(f"  copia registrada {copia_arn}")
    else:
        paso["copia"] = False
    paso["task_definition_usada"] = usar
    started_by = f"reset-c{corrida}"
    if dry_run:
        log(f"  run-task FARGATE {FIJO_CLUSTER} td={usar} started-by={started_by} red={json.dumps(red['awsvpcConfiguration'])}")
        log(f"  override {json.dumps(overrides_reset())}")
        paso.update(resultado="dry-run", fin=ahora())
        return paso
    try:
        r = aws(PERFIL_FIJO, "ecs", "run-task", "--cluster", FIJO_CLUSTER, "--task-definition", usar,
                "--launch-type", "FARGATE", "--network-configuration", json.dumps(red),
                "--overrides", json.dumps(overrides_reset()), "--started-by", started_by, "--count", "1")
        if r.get("failures"):
            raise ErrorReset(f"run-task falló: {r['failures']}")
        task_arn = r["tasks"][0]["taskArn"]
        task_id = task_arn.rsplit("/", 1)[1]
        paso.update(task_arn=task_arn, task_id=task_id)
        log(f"  tarea {task_id} lanzada; esperando que pare")
        for intento in range(6):  # el waiter corta a los ~10 min
            try:
                aws(PERFIL_FIJO, "ecs", "wait", "tasks-stopped", "--cluster", FIJO_CLUSTER, "--tasks", task_arn, salida_json=False)
                break
            except ErrorReset:
                if intento == 5:
                    raise
        t = aws(PERFIL_FIJO, "ecs", "describe-tasks", "--cluster", FIJO_CLUSTER, "--tasks", task_arn, "--query", "tasks[0]")
        c = next(x for x in t["containers"] if x["name"] == FIJO_CONTENEDOR)
        paso.update(exit_code=c.get("exitCode"), stop_code=t.get("stopCode"), stopped_reason=t.get("stoppedReason"),
                    started_at=t.get("startedAt"), stopped_at=t.get("stoppedAt"))
        lineas, reporte = [], None
        for _ in range(12):  # los logs pueden tardar unos segundos en aparecer
            try:
                lineas = _logs_tarea(task_id)
            except ErrorReset:
                lineas = []
            reporte = extraer_reporte(lineas)
            if reporte is not None:
                break
            time.sleep(5)
        paso["log_ultimas"] = [redactar(x) for x in lineas[-LINEAS_LOG:]]
        paso["reporte"] = reporte
        if c.get("exitCode") != 0:
            raise ErrorReset(f"reset_baseline.py terminó con exit {c.get('exitCode')} ({t.get('stoppedReason')})")
        if not (reporte and reporte.get("baseline_ok") is True):
            raise ErrorReset(f"el reporte del reset no dice baseline_ok: {reporte}")
        paso["resultado"] = "ok"
    finally:
        if copia_arn:
            aws(PERFIL_FIJO, "ecs", "deregister-task-definition", "--task-definition", copia_arn, "--query", "taskDefinition.status")
            paso["copia_desregistrada"] = copia_arn
            log(f"  copia {copia_arn} desregistrada")
        paso["fin"] = ahora()
    return paso


def _ultimo_ids(proyecto):
    return set(aws(PERFIL_FIJO, "codebuild", "list-builds-for-project", "--project-name", proyecto, "--query", "ids[0:10]") or [])


def _log_build(build):
    g, s = build["logs"]["groupName"], build["logs"]["streamName"]
    lineas, token = [], None
    while True:
        args = ["logs", "get-log-events", "--log-group-name", g, "--log-stream-name", s, "--start-from-head"]
        if token:
            args += ["--next-token", token]
        r = aws(PERFIL_FIJO, *args)
        lineas += [e["message"].rstrip("\n") for e in r["events"]]
        if r["nextForwardToken"] == token:
            break
        token = r["nextForwardToken"]
    return lineas


def desplegar_cd(paso, corrida, cd, proyecto, dry_run):
    paso.update(inicio=ahora(), repo=cd, proyecto=proyecto)
    clon = acciones.preparar_clon(cd, TRABAJO, f"https://github.com/{ORG}/{cd}.git")
    git = lambda *a: acciones._git(clon, *a)  # noqa: E731
    tag_main = _correr(["git", "show", "origin/main:tag.json"], cwd=clon)
    tag_dev = _correr(["git", "show", "origin/dev:tag.json"], cwd=clon)
    paso.update(main=git("rev-parse", "origin/main"), dev_previo=git("rev-parse", "origin/dev"), cambia_tag=tag_main != tag_dev)
    mensaje = f"reset c{corrida}: linea base"
    if dry_run:
        log(f"  {cd}: dev {paso['dev_previo'][:7]} -> commit '{mensaje}' "
            f"({'cambia tag.json' if paso['cambia_tag'] else '--allow-empty, tag.json ya es el de main'}); push a dev; "
            f"esperar {proyecto}")
        paso.update(resultado="dry-run", fin=ahora())
        return paso
    git("checkout", "--quiet", "-B", "dev", "origin/dev")
    (clon / "tag.json").write_text(tag_main, encoding="utf-8")
    git("add", "tag.json")
    git("commit", "--quiet", "--allow-empty", "-m", mensaje)
    commit = git("rev-parse", "HEAD")
    previos = _ultimo_ids(proyecto)
    git("push", "--quiet", "origin", "HEAD:refs/heads/dev")
    paso.update(commit_dev=commit, push=ahora())
    log(f"  {cd}: push de {commit[:7]} a dev; esperando el build de {proyecto}")

    t0, build = time.time(), None
    while time.time() - t0 < MAX_BUILD_SEG:
        if build is None:
            ids = aws(PERFIL_FIJO, "codebuild", "list-builds-for-project", "--project-name", proyecto, "--query", "ids[0:5]") or []
            nuevos = [i for i in ids if i not in previos]
            if nuevos:
                bs = aws(PERFIL_FIJO, "codebuild", "batch-get-builds", "--ids", *nuevos, "--query", "builds")
                build = elegir_build(bs, commit, previos)
                if build:
                    paso["build_id"] = build["id"]
                    log(f"  build {build['id']}")
        if build is not None:
            build = aws(PERFIL_FIJO, "codebuild", "batch-get-builds", "--ids", build["id"], "--query", "builds[0]")
            if build["buildComplete"]:
                break
        time.sleep(SONDEO_SEG)
    else:
        raise ErrorReset(f"{proyecto}: sin build terminado a los {MAX_BUILD_SEG // 60} min ({paso.get('build_id')})")

    paso.update(build_status=build["buildStatus"], build_inicio=build["startTime"], build_fin=build["endTime"])
    if build["buildStatus"] != "SUCCEEDED":
        paso["fin"] = ahora()
        raise ErrorReset(f"{build['id']} terminó {build['buildStatus']}")
    time.sleep(10)  # que el log termine de llegar
    lineas = _log_build(build)
    uri = None
    for x in lineas:
        m = re.search(r"Registro (s3://\S+/validacion\.json)", x)
        if m:
            uri = m.group(1)
    if not uri:
        raise ErrorReset(f"{build['id']}: no encontré la línea 'Registro s3://.../validacion.json' en el log")
    v = json.loads(aws(PERFIL_FIJO, "s3", "cp", uri, "-", "--only-show-errors", salida_json=False))
    suite = v.get("suite") or {}
    paso.update(validacion=uri, build_ok=v.get("build_ok"), imagen=v.get("imagen"), despliegue=v.get("despliegue"),
                suite={k: suite.get(k) for k in ("resultado", "total", "total_esperado", "exit_code", "suite_seg")})
    fallos = validar_validacion(v)
    paso["fin"] = ahora()
    if fallos:
        raise ErrorReset(f"{build['id']}: {'; '.join(fallos)}")
    paso["resultado"] = "ok"
    return paso


def ramas_cierre(paso, dry_run):
    paso.update(inicio=ahora(), repos={})
    for cd, _ in CDS:
        main = ref_sha(cd, "main")
        estado = {"main": main}
        for rama in RAMAS_CIERRE:
            actual = ref_sha(cd, rama)
            accion = accion_rama(actual, main)
            estado[rama] = {"antes": actual, "accion": accion}
            if dry_run:
                log(f"  {cd} {rama}: {actual and actual[:7]} -> {main[:7]} ({accion})")
                continue
            if accion == "crear":
                gh_api(f"repos/{ORG}/{cd}/git/refs", "-X", "POST", "-f", f"ref=refs/heads/{rama}", "-f", f"sha={main}")
            elif accion == "actualizar":
                gh_api(f"repos/{ORG}/{cd}/git/refs/heads/{rama}", "-X", "PATCH", "-f", f"sha={main}", "-F", "force=true")
            despues = ref_sha(cd, rama)
            if despues != main:
                raise ErrorReset(f"{cd} {rama} quedó en {despues}, no en main {main}")
            estado[rama]["despues"] = despues
            log(f"  {cd} {rama}: {accion} -> {despues[:7]}")
        paso["repos"][cd] = estado
    paso.update(resultado="dry-run" if dry_run else "ok", fin=ahora())
    return paso


def shas_finales():
    return {cd: {r: ref_sha(cd, r) for r in ["main", "dev", *RAMAS_CIERRE]} for cd, _ in CDS}


# --------------------------------------------------------------------------------------------------


def reset(corrida: int, dry_run: bool) -> int:
    reg = {"corrida": f"c{corrida}", "dry_run": dry_run, "inicio": ahora(), "pasos": {}}
    salida = REGISTROS / f"reset-c{corrida}.json"

    def guardar():
        if not dry_run:
            REGISTROS.mkdir(parents=True, exist_ok=True)
            salida.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    actual = "precondiciones"
    try:
        log("1. precondiciones")
        p = {"inicio": ahora()}
        datos = recoger_precondiciones()
        fallos = validar_precondiciones(datos)
        p.update(builds={k: len(v) for k, v in datos["builds"].items()}, fallos=fallos, fin=ahora(),
                 resultado="ok" if not fallos else "fallo")
        reg["pasos"]["precondiciones"] = p
        for f in fallos:
            log(f"  FALLA: {f}")
        if fallos:
            raise ErrorReset("precondiciones no cumplidas; no se tocó nada")
        log("  ok")

        tag_backend = tag_json_main("tesis-backend-cd")
        imagen_base = f"{tag_backend['repo']}@{tag_backend['digest']}"
        reg["linea_base"] = {"backend": imagen_base,
                             "frontend": (lambda t: f"{t['repo']}@{t['digest']}")(tag_json_main("tesis-frontend-cd"))}
        log(f"  línea base backend {imagen_base}")

        actual = "reset_1"
        log("2. primer reset de datos")
        reset_datos(reg["pasos"].setdefault("reset_1", {}), corrida, imagen_base, dry_run, permitir_copia=True)

        for cd, proyecto in CDS:
            actual = f"despliegue_{cd}"
            log(f"3. despliegue de la línea base en dev de {cd}")
            desplegar_cd(reg["pasos"].setdefault(actual, {}), corrida, cd, proyecto, dry_run)

        actual = "reset_2"
        log("4. segundo reset de datos")
        reset_datos(reg["pasos"].setdefault("reset_2", {}), corrida, imagen_base, dry_run, permitir_copia=False)

        actual = "ramas_cierre"
        log("5. ramas de cierre")
        ramas_cierre(reg["pasos"].setdefault("ramas_cierre", {}), dry_run)

        reg["shas_finales"] = shas_finales()
        reg["fin"] = ahora()
        reg["t0_corrida"] = reg["fin"]
        reg["resultado"] = "dry-run" if dry_run else "ok"
        guardar()
        log(f"reset c{corrida} {'(dry-run) ' if dry_run else ''}terminado; t0 = {reg['fin']}")
        if dry_run:
            print(json.dumps(reg, indent=2, ensure_ascii=False))
        else:
            log(f"registro en {salida}")
        return 0
    except (ErrorReset, acciones.ErrorAccion) as error:
        reg["resultado"] = "fallo"
        reg["fallo"] = {"paso": actual, "motivo": str(error), "hora": ahora()}
        paso = reg["pasos"].setdefault(actual, {})
        if paso.get("resultado") != "ok":
            paso["resultado"] = "fallo"
        paso.setdefault("fin", ahora())
        try:
            reg["shas_finales"] = shas_finales()
        except Exception:  # noqa: BLE001
            pass
        reg["fin"] = ahora()
        guardar()
        log(f"ERROR en {actual}: {error}")
        if dry_run:
            print(json.dumps(reg, indent=2, ensure_ascii=False))
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reset de inicio de corrida del entorno compartido del fijo")
    parser.add_argument("--corrida", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return reset(args.corrida, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
