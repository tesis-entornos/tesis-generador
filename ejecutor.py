#!/usr/bin/env python3
"""Ejecutor de una corrida completa en los dos modelos (fijo y efímero).

    python ejecutor.py --escenario escenarios/c<N>.json [--sin-reset] [--dry-run]

Un bucle de un solo hilo que cada 15 s:
  1. (al inicio) corre reset.py salvo --sin-reset; su fin es t = 0;
  2. despacha los eventos de eventos_ordenados cuyo t ya llegó (acciones.abrir / acciones.actualizar). Un
     evento espera a que termine el build único del evento anterior de la misma versión y repo, para que dos
     builds únicos no escriban a la vez el tag.json de la misma rama del -cd;
  3. consulta builds (build único, fijo, efímero, cierre), PRs del -cd -> dev y registros (validacion.json,
     destruccion.json);
  4. cola del fijo: con el fijo libre, integra en dev el PR -cd -> dev abierto más antiguo (merge --no-ff,
     conflicto de tag.json resuelto con --theirs) y espera su build del fijo;
  5. validación final de cada versión en cada modelo: para cada repo, una validación terminada con el digest
     más reciente; la final es la última en terminar;
  6. cierre en cada modelo apenas hay validación final (efímero: PR -> main-efimero mergeado localmente o
     cerrado, build de cierre del webhook o manual; fijo: PR -> main-fijo mergeado localmente o cerrado);
  7. limpieza de la versión cuando cerró en los dos modelos y no hay cierres en curso;
  8. registro final en registros/corrida-c<N>.json.

Estado en registros/estado-c<N>.json (cada vuelta) y log legible en registros/c<N>.log. Si algo espera más
de 90 min, la corrida se aborta como inválida (sin limpiar). --dry-run recorre el escenario contra una
plataforma simulada con reloj virtual, sin tocar nada.
"""

import argparse
import base64
import hashlib
import heapq
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import acciones
import reset as reset_mod

RAIZ = Path(__file__).resolve().parent
REGISTROS = RAIZ / "registros"
TRABAJO = RAIZ / ".trabajo"
ORG = "tesis-entornos"
REGION = "us-east-2"

VUELTA_SEG = 15
MAX_ESPERA_SEG = 90 * 60
WEBHOOK_SEG = 120
MERGED_SEG = 180
MODELOS = ("efimero", "fijo")

PERFILES = {"tesis-build-": "tf-compartida", "tesis-efimero-": "tf-efimero", "tesis-fijo-": "tf-fijo"}
BUCKET_REGISTROS = {"efimero": ("tf-efimero", "tesis-efimero-registros-398846004421"),
                    "fijo": ("tf-fijo", "tesis-fijo-registros-491265021424")}


class ErrorEjecutor(Exception):
    pass


class Abortar(Exception):
    pass


def iso(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def epoch(texto):
    if texto is None:
        return None
    if isinstance(texto, (int, float)):
        return float(texto)
    return datetime.fromisoformat(texto.replace("Z", "+00:00")).timestamp()


def cd_de(repo):
    return f"tesis-{repo}-cd"


def app_de(repo):
    return f"tesis-{repo}"


def proyecto(tipo, repo):
    return {"unico": f"tesis-build-{repo}", "efimero": f"tesis-efimero-desplegar-{repo}",
            "fijo": f"tesis-fijo-desplegar-{repo}", "cierre": f"tesis-efimero-cerrar-{repo}"}[tipo]


# --------------------------------------------------------------------------------------------------
# Decisiones (puras)


def elegir_pr_cola(prs: list, integrados: dict):
    """PR -> dev abierto más antiguo (createdAt) cuya punta todavía no se integró."""
    candidatos = [p for p in prs if p["sha"] not in integrados.get(f"{p['cd']}#{p['number']}", [])]
    if not candidatos:
        return None
    return min(candidatos, key=lambda p: (p["created"], p["cd"], p["number"]))


def validacion_final(repos: list, digests: dict, validaciones: list):
    """digests = {repo: digest más reciente}; validaciones = [{repo, digest, build_id, end, resultado}] de un
    modelo y una rama. Devuelve la validación final o None si falta alguna."""
    elegidas = {}
    for repo in repos:
        if repo not in digests:
            return None
        hechas = [v for v in validaciones if v["repo"] == repo and v["digest"] == digests[repo] and v["end"] is not None]
        if not hechas:
            return None
        elegidas[repo] = min(hechas, key=lambda v: v["end"])  # la primera con ese digest
    final = max(elegidas.values(), key=lambda v: v["end"])
    return {"build_id": final["build_id"], "repo": final["repo"], "end": final["end"], "resultado": final["resultado"],
            "por_repo": {r: {"build_id": v["build_id"], "end": v["end"], "resultado": v["resultado"]} for r, v in elegidas.items()}}


def esperar_webhook(t_accion: float, ahora: float, build) -> str:
    """'seguir' si el build del webhook apareció, 'esperar' dentro de los 120 s, 'manual' después."""
    if build is not None:
        return "seguir"
    return "esperar" if ahora - t_accion < WEBHOOK_SEG else "manual"


def destruido(resultados: list) -> bool:
    """resultados = [{accion, resultado}] de los builds de cierre de una versión."""
    return any(r.get("accion") in ("destruir", "manual") and r.get("resultado") in ("ok", "nada que destruir")
               for r in resultados)


def decidir_cierre(resultados: list, repos: list) -> str:
    """Tras cerrar todos los repos: 'hecho' si alguno destruyó; 'manual' si todos esperan al otro; 'falla' si no."""
    if destruido(resultados):
        return "hecho"
    if len(resultados) == len(repos) and all(r.get("accion") == "esperar_otro" for r in resultados):
        return "manual"
    return "falla"


def merge_con_tag(clon, ref: str, mensaje: str) -> str:
    """git merge --no-ff de ref; un conflicto solo en tag.json se resuelve con --theirs. Devuelve
    'sin_conflicto' o 'tag.json'; cualquier otro conflicto aborta el merge y levanta ErrorEjecutor."""
    def git(*a, check=True):
        r = subprocess.run(["git", *a], cwd=clon, text=True, capture_output=True)
        if check and r.returncode != 0:
            raise ErrorEjecutor(f"git {' '.join(a)}: {(r.stderr or r.stdout).strip()[:400]}")
        return r

    r = git("merge", "--no-ff", "--no-edit", "-m", mensaje, ref, check=False)
    if r.returncode == 0:
        return "sin_conflicto"
    conflictos = [x for x in git("diff", "--name-only", "--diff-filter=U").stdout.split("\n") if x]
    if conflictos == ["tag.json"]:
        git("checkout", "--theirs", "tag.json")
        git("add", "tag.json")
        git("commit", "--no-edit", "-m", mensaje)
        return "tag.json"
    git("merge", "--abort", check=False)
    raise ErrorEjecutor(f"merge de {ref}: conflicto fuera de tag.json ({conflictos or (r.stderr or r.stdout).strip()[:300]})")


def resultado_fijo(v: dict) -> str:
    ok = (v.get("build_ok") is True and v.get("despliegue") == "ok"
          and (v.get("espera") or {}).get("resultado") == "ok" and (v.get("suite") or {}).get("resultado") == "ok")
    return "ok" if ok else "falla"


# --------------------------------------------------------------------------------------------------
# Plataforma real


def _correr(args, cwd=None, check=True):
    r = subprocess.run(args, cwd=cwd, text=True, capture_output=True, env=reset_mod._entorno())
    if check and r.returncode != 0:
        raise ErrorEjecutor(f"falló {' '.join(args[:5])}: {reset_mod.redactar((r.stderr or r.stdout).strip())[:600]}")
    return r


class PlataformaReal:
    simulada = False

    def __init__(self, escenario):
        self.escenario = escenario
        self.corrida = escenario["corrida"]
        self._cache = {}
        self._docs = {}

    def ahora(self):
        return time.time()

    def dormir(self, seg):
        if seg > 0:
            time.sleep(seg)

    def reset(self):
        if reset_mod.reset(self.corrida, False) != 0:
            raise Abortar(f"reset.py falló (ver registros/reset-c{self.corrida}.json)")
        reg = json.loads((REGISTROS / f"reset-c{self.corrida}.json").read_text(encoding="utf-8"))
        return epoch(reg["fin"])

    def abrir(self, n, repo):
        r, _ = acciones.abrir(self.escenario, n, repo, False)
        return {"commit": r["commit"], "pr": r["pr"], "pr_created_at": r["pr_created_at"]}

    def actualizar(self, n, repo, k):
        r, _ = acciones.actualizar(self.escenario, n, repo, k, False)
        return {"commit": r["commit"]}

    def _aws(self, perfil, *args):
        out = _correr(["aws", *args, "--profile", perfil, "--region", REGION, "--output", "json"]).stdout
        return json.loads(out) if out.strip() else None

    def builds(self, proy, desde):
        perfil = next(p for pre, p in PERFILES.items() if proy.startswith(pre))
        ids = self._aws(perfil, "codebuild", "list-builds-for-project", "--project-name", proy, "--query", "ids[0:30]") or []
        pedir = [i for i in ids if not (i in self._cache and self._cache[i]["complete"])]
        if pedir:
            for b in self._aws(perfil, "codebuild", "batch-get-builds", "--ids", *pedir, "--query", "builds") or []:
                self._cache[b["id"]] = {"id": b["id"], "status": b["buildStatus"], "complete": b["buildComplete"],
                                        "start": epoch(b["startTime"]), "end": epoch(b.get("endTime")),
                                        "resolved": b.get("resolvedSourceVersion"), "source": b.get("sourceVersion")}
        return [self._cache[i] for i in ids if i in self._cache and self._cache[i]["start"] >= desde - 1]

    def _s3_json(self, modelo, clave):
        perfil, bucket = BUCKET_REGISTROS[modelo]
        r = _correr(["aws", "s3", "cp", f"s3://{bucket}/{clave}", "-", "--only-show-errors", "--profile", perfil,
                     "--region", REGION], check=False)
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None

    def validacion(self, modelo, repo, build_id):
        clave = ("val", modelo, build_id)
        if clave not in self._docs:
            v = self._s3_json(modelo, f"validaciones/corrida=c{self.corrida}/{build_id.split(':', 1)[1]}/validacion.json")
            if v is None:
                return None
            if modelo == "efimero":
                d = {"rama": v.get("rama"), "digest": v.get("digest"), "resultado": "ok" if v.get("resultado") == "ok" else "falla",
                     "detalle": v.get("resultado")}
            else:
                imagen = v.get("imagen") or ""
                d = {"rama": v.get("rama"), "digest": imagen.split("@", 1)[1] if "@" in imagen else None,
                     "resultado": resultado_fijo(v),
                     "detalle": {"build_ok": v.get("build_ok"), "despliegue": v.get("despliegue"),
                                 "espera": (v.get("espera") or {}).get("resultado"), "suite": (v.get("suite") or {}).get("resultado")}}
            self._docs[clave] = d
        return self._docs[clave]

    def destruccion(self, build_id):
        clave = ("des", build_id)
        if clave not in self._docs:
            d = self._s3_json("efimero", f"destrucciones/corrida=c{self.corrida}/{build_id.split(':', 1)[1]}/destruccion.json")
            if d is None:
                return None
            c = d.get("cierre") or {}
            self._docs[clave] = {"accion": c.get("accion"), "resultado": d.get("resultado"), "cierre_resultado": c.get("resultado"),
                                 "otra_rama_existe": c.get("otra_rama_existe"), "evento": c.get("evento"), "rama": d.get("rama")}
        return self._docs[clave]

    def prs_dev(self):
        prs = []
        for repo in ("backend", "frontend"):
            cd = cd_de(repo)
            lista = json.loads(_correr(["gh", "pr", "list", "-R", f"{ORG}/{cd}", "--base", "dev", "--state", "open",
                                        "--json", "number,headRefName,headRefOid,createdAt"]).stdout)
            prs += [{"cd": cd, "repo": repo, "number": p["number"], "head": p["headRefName"], "sha": p["headRefOid"],
                     "created_at": p["createdAt"], "created": epoch(p["createdAt"])} for p in lista]
        return prs

    def tag_json(self, cd, rama):
        r = _correr(["gh", "api", f"repos/{ORG}/{cd}/contents/tag.json?ref={rama}"], check=False)
        if r.returncode != 0:
            return None
        return json.loads(base64.b64decode(json.loads(r.stdout)["content"]))

    def integrar(self, cd, rama, base, mensaje):
        clon = acciones.preparar_clon(cd, TRABAJO, f"https://github.com/{ORG}/{cd}.git")
        _correr(["git", "merge", "--abort"], cwd=clon, check=False)
        _correr(["git", "checkout", "--quiet", "-f", "-B", base, f"origin/{base}"], cwd=clon)
        conflicto = merge_con_tag(clon, f"origin/{rama}", mensaje)
        commit = _correr(["git", "rev-parse", "HEAD"], cwd=clon).stdout.strip()
        _correr(["git", "push", "--quiet", "origin", f"HEAD:refs/heads/{base}"], cwd=clon)
        return {"commit": commit, "conflicto": conflicto}

    def pr_crear(self, cd, base, rama, titulo):
        r = _correr(["gh", "pr", "create", "-R", f"{ORG}/{cd}", "--base", base, "--head", rama, "--title", titulo,
                     "--body", f"ejecutor c{self.corrida}"], check=False)
        if r.returncode != 0:
            ya = json.loads(_correr(["gh", "pr", "list", "-R", f"{ORG}/{cd}", "--base", base, "--head", rama, "--state", "open",
                                     "--json", "number"]).stdout)
            if ya:
                return ya[0]["number"]
            raise ErrorEjecutor(f"gh pr create {cd} {rama} -> {base}: {(r.stderr or r.stdout).strip()[:300]}")
        return int(r.stdout.strip().rsplit("/", 1)[1])

    def pr_estado(self, repo_gh, n):
        d = json.loads(_correr(["gh", "pr", "view", str(n), "-R", f"{ORG}/{repo_gh}", "--json", "state,mergedAt"]).stdout)
        return {"state": d["state"], "merged": d["state"] == "MERGED", "merged_at": d.get("mergedAt")}

    def pr_cerrar(self, repo_gh, n):
        _correr(["gh", "pr", "close", str(n), "-R", f"{ORG}/{repo_gh}"])

    def cierre_manual(self, repo, rama):
        b = self._aws("tf-efimero", "codebuild", "start-build", "--project-name", proyecto("cierre", repo),
                      "--environment-variables-override", f"name=RAMA,value={rama},type=PLAINTEXT", "--query", "build.id")
        return b

    def borrar_rama(self, repo_gh, rama):
        r = _correr(["gh", "api", "-X", "DELETE", f"repos/{ORG}/{repo_gh}/git/refs/heads/{rama}"], check=False)
        if r.returncode != 0 and "Reference does not exist" not in (r.stdout + r.stderr) and "Not Found" not in (r.stdout + r.stderr):
            raise ErrorEjecutor(f"no se pudo borrar {repo_gh} {rama}: {(r.stderr or r.stdout).strip()[:300]}")
        return r.returncode == 0


# --------------------------------------------------------------------------------------------------
# Plataforma simulada (dry-run y pruebas): reloj virtual, duraciones típicas medidas en las pruebas reales


DUR = {"unico": 90, "efimero_nuevo": 640, "efimero": 330, "fijo_backend": 330, "fijo_frontend": 141,
       "webhook": 20, "esperar_otro": 25, "destruir": 360}


class PlataformaSimulada:
    simulada = True

    def __init__(self, escenario, inicio=None):
        self.escenario = escenario
        self.corrida = escenario["corrida"]
        self.t = inicio if inicio is not None else float(int(time.time()))
        self._cola, self._seq = [], 0
        self.b = {}           # proyecto -> [build]
        self.val, self.des = {}, {}
        self.prs = {}         # repo_gh -> [pr]
        self.tags = {}        # (cd, rama) -> tag
        self.ramas = set()    # (repo_gh, rama)
        self.entornos = set()
        self.fin_rama = {}    # rama -> fin del último despliegue efímero (serialización)
        self.n = 0

    def _h(self, *partes):
        self.n += 1
        return hashlib.sha1(("|".join(map(str, partes)) + str(self.n)).encode()).hexdigest()

    def _programar(self, t, fn):
        self._seq += 1
        heapq.heappush(self._cola, (t, self._seq, fn))

    def _avanzar(self):
        while self._cola and self._cola[0][0] <= self.t:
            _, _, fn = heapq.heappop(self._cola)
            fn()

    def ahora(self):
        return self.t

    def dormir(self, seg):
        self.t += seg
        self._avanzar()

    def reset(self):
        self.t += 690  # duración del reset real de c97
        return self.t

    def _build(self, proy, inicio, dur, resolved, source, al_terminar=None):
        b = {"id": f"{proy}:{self._h(proy)[:8]}-sim", "status": "IN_PROGRESS", "complete": False, "start": inicio, "end": None,
             "resolved": resolved, "source": source}
        self.b.setdefault(proy, []).insert(0, b)

        def fin():
            b.update(status="SUCCEEDED", complete=True, end=inicio + dur)
            if al_terminar:
                al_terminar(b)
        self._programar(inicio + dur, fin)
        return b

    def _pr(self, repo_gh, base, head, sha=None):
        lista = self.prs.setdefault(repo_gh, [])
        p = {"number": len(lista) + 1, "base": base, "head": head, "state": "OPEN", "sha": sha, "created": self.t}
        lista.append(p)
        return p

    def _evento_app(self, n, repo, pr):
        v = acciones.version(self.escenario, n)
        rama, cd = v["rama"], cd_de(repo)
        commit = self._h("app", n, repo)

        def tras_unico(b):
            digest = "sha256:" + hashlib.sha256(commit.encode()).hexdigest()
            self.tags[(cd, rama)] = {"commit": commit, "digest": digest, "rama": rama}
            self.ramas.add((cd, rama))
            cd_commit = self._h("cd", rama)
            abierto = [p for p in self.prs.get(cd, []) if p["head"] == rama and p["base"] == "dev" and p["state"] == "OPEN"]
            if abierto:
                abierto[0]["sha"] = cd_commit
            else:
                self._pr(cd, "dev", rama, cd_commit)
            inicio = max(self.t + 1, self.fin_rama.get(rama, 0))
            dur = DUR["efimero"] if rama in self.entornos else DUR["efimero_nuevo"]
            self.entornos.add(rama)
            self.fin_rama[rama] = inicio + dur
            e = self._build(proyecto("efimero", repo), inicio, dur, cd_commit, cd_commit)
            self.val[e["id"]] = {"rama": rama, "digest": digest, "resultado": "ok", "detalle": "ok"}
        self._build(proyecto("unico", repo), self.t + 2, DUR["unico"], commit, f"pr/{pr}", tras_unico)
        return commit

    def abrir(self, n, repo):
        rama = acciones.version(self.escenario, n)["rama"]
        p = self._pr(app_de(repo), "main", rama)
        self.ramas.add((app_de(repo), rama))
        commit = self._evento_app(n, repo, p["number"])
        return {"commit": commit, "pr": p["number"], "pr_created_at": iso(self.t)}

    def actualizar(self, n, repo, k):
        rama = acciones.version(self.escenario, n)["rama"]
        p = next(x for x in self.prs[app_de(repo)] if x["head"] == rama)
        return {"commit": self._evento_app(n, repo, p["number"])}

    def builds(self, proy, desde):
        return [dict(b) for b in self.b.get(proy, []) if b["start"] >= desde - 1 and b["start"] <= self.t]

    def validacion(self, modelo, repo, build_id):
        return self.val.get(build_id)

    def destruccion(self, build_id):
        return self.des.get(build_id)

    def prs_dev(self):
        out = []
        for repo in ("backend", "frontend"):
            cd = cd_de(repo)
            out += [{"cd": cd, "repo": repo, "number": p["number"], "head": p["head"], "sha": p["sha"],
                     "created_at": iso(p["created"]), "created": p["created"]}
                    for p in self.prs.get(cd, []) if p["base"] == "dev" and p["state"] == "OPEN"]
        return out

    def tag_json(self, cd, rama):
        return self.tags.get((cd, rama))

    def _cierre_webhook(self, cd, rama, pr, evento):
        repo = cd.split("-")[1]
        otro = cd_de("frontend" if repo == "backend" else "backend")
        otra = (otro, rama) in self.ramas
        accion = "esperar_otro" if otra else "destruir"
        b = self._build(proyecto("cierre", repo), self.t + DUR["webhook"], DUR[accion], None, f"pr/{pr}")
        self.des[b["id"]] = {"accion": accion, "resultado": "esperando el cierre del otro -cd" if otra else "ok",
                             "otra_rama_existe": otra, "evento": evento, "rama": rama}

    def integrar(self, cd, rama, base, mensaje):
        commit = self._h("merge", cd, base)
        for p in self.prs.get(cd, []):
            if p["head"] == rama and p["base"] == base and p["state"] == "OPEN":
                p["state"] = "MERGED"
                if base == "main-efimero":
                    self._cierre_webhook(cd, rama, p["number"], "PULL_REQUEST_MERGED")
        if base == "dev":
            repo = cd.split("-")[1]
            tag = self.tags[(cd, rama)]
            f = self._build(proyecto("fijo", repo), self.t + 3, DUR[f"fijo_{repo}"], commit, commit)
            self.val[f["id"]] = {"rama": rama, "digest": tag["digest"], "resultado": "ok", "detalle": "ok"}
        return {"commit": commit, "conflicto": "tag.json"}

    def pr_crear(self, cd, base, rama, titulo):
        return self._pr(cd, base, rama)["number"]

    def pr_estado(self, repo_gh, n):
        p = self.prs[repo_gh][n - 1]
        return {"state": p["state"], "merged": p["state"] == "MERGED", "merged_at": None}

    def pr_cerrar(self, repo_gh, n):
        p = self.prs[repo_gh][n - 1]
        p["state"] = "CLOSED"
        if p["base"] == "main-efimero":
            self._cierre_webhook(repo_gh, p["head"], n, "PULL_REQUEST_CLOSED")

    def cierre_manual(self, repo, rama):
        b = self._build(proyecto("cierre", repo), self.t + 1, DUR["destruir"], None, "main")
        self.des[b["id"]] = {"accion": "manual", "resultado": "ok", "otra_rama_existe": None, "evento": None, "rama": rama}
        return b["id"]

    def borrar_rama(self, repo_gh, rama):
        self.ramas.discard((repo_gh, rama))
        return True


# --------------------------------------------------------------------------------------------------
# Ejecutor


class Ejecutor:
    def __init__(self, escenario, plataforma, sin_reset=False, escribir=True):
        self.e = escenario
        self.p = plataforma
        self.c = escenario["corrida"]
        self.sin_reset = sin_reset
        self.escribir = escribir
        self.archivo_estado = REGISTROS / f"estado-c{self.c}.json"
        self.archivo_log = REGISTROS / f"c{self.c}.log"
        self.archivo_corrida = REGISTROS / f"corrida-c{self.c}.json"
        self.s = {
            "corrida": f"c{self.c}", "semilla": escenario["semilla"], "nivel": escenario["nivel"],
            "simulada": plataforma.simulada, "inicio": None, "t0": None, "fin": None, "resultado": None, "motivo": None,
            "eventos": [dict(ev, i=i, estado="pendiente") for i, ev in enumerate(escenario["eventos_ordenados"])],
            "versiones": {},
            "cola_fijo": {"en_curso": None, "integraciones": []},
            "integrados": {},
            "merged_pendientes": [],
            "esperas": {},
            "vueltas": 0,
        }
        for v in escenario["versiones"]:
            self.s["versiones"][str(v["n"])] = {
                "n": v["n"], "rama": v["rama"], "tipo": v["tipo"], "repos": v["repos"], "resultado": v["resultado"],
                "app_pr": {}, "apertura": None, "digest": {}, "validaciones": {"efimero": [], "fijo": []},
                "final": {"efimero": None, "fijo": None},
                "cierre": {"efimero": {"estado": "pendiente", "repos": {}, "manual_extra": None, "destruccion": None},
                           "fijo": {"estado": "pendiente", "repos": {}}},
                "limpieza": None,
            }
        self.builds = {}

    # --- utilidades -----------------------------------------------------------------------------

    def t(self):
        t0 = self.s["t0"]
        if t0 is None:
            return "  pre"
        d = int(self.p.ahora() - t0)
        return f"{'-' if d < 0 else '+'}{abs(d) // 60:02d}:{abs(d) % 60:02d}"

    def log(self, msg):
        linea = f"[{iso(self.p.ahora())}] t{self.t()}  {msg}"
        print(linea, flush=True)
        if self.escribir:
            REGISTROS.mkdir(parents=True, exist_ok=True)
            with self.archivo_log.open("a", encoding="utf-8") as f:
                f.write(linea + "\n")

    def aviso_consulta(self, error):
        self.s["errores_consulta"] = self.s.get("errores_consulta", 0) + 1
        self.log(f"AVISO consulta fallida ({self.s['errores_consulta']} seguidas): {error}")
        if self.s["errores_consulta"] > 10:
            raise Abortar(f"más de 10 consultas seguidas fallidas; la última: {error}")

    def esperar(self, clave, que):
        if clave not in self.s["esperas"]:
            self.s["esperas"][clave] = {"desde": self.p.ahora(), "que": que}

    def listo(self, clave):
        self.s["esperas"].pop(clave, None)

    def guardar(self):
        if self.escribir:
            REGISTROS.mkdir(parents=True, exist_ok=True)
            tmp = self.archivo_estado.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.s, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(self.archivo_estado)

    def V(self, n):
        return self.s["versiones"][str(n)]

    def eventos_de(self, n, repo=None):
        return [ev for ev in self.s["eventos"] if ev["n"] == n and (repo is None or ev["repo"] == repo)]

    # --- consultas ------------------------------------------------------------------------------

    def refrescar(self):
        desde = self.s["t0"]
        hay_cierres = any(v["cierre"]["efimero"]["estado"] not in ("pendiente",) for v in self.s["versiones"].values())
        for repo in ("backend", "frontend"):
            tipos = ["efimero", "fijo"]
            if any(ev["estado"] == "despachado" for ev in self.s["eventos"]):
                tipos.append("unico")
            if hay_cierres:
                tipos.append("cierre")
            for tipo in ("unico", "efimero", "fijo", "cierre"):
                if tipo in tipos:
                    self.builds[proyecto(tipo, repo)] = self.p.builds(proyecto(tipo, repo), desde)
        # validaciones terminadas de los dos modelos
        for modelo in MODELOS:
            for repo in ("backend", "frontend"):
                for b in self.builds.get(proyecto(modelo, repo), []):
                    if not b["complete"]:
                        continue
                    ya = any(x["build_id"] == b["id"] for v in self.s["versiones"].values() for x in v["validaciones"][modelo])
                    if ya or b["id"] in self.s.setdefault("sin_registro", {}) and self.s["sin_registro"][b["id"]] >= 8:
                        continue
                    d = self.p.validacion(modelo, repo, b["id"])
                    if d is None:
                        self.s["sin_registro"][b["id"]] = self.s["sin_registro"].get(b["id"], 0) + 1
                        if self.s["sin_registro"][b["id"]] == 8:
                            self.log(f"AVISO {b['id']} ({b['status']}) terminó sin validacion.json legible; no cuenta")
                        continue
                    v = next((x for x in self.s["versiones"].values() if x["rama"] == d["rama"]), None)
                    if v is None:
                        self.log(f"AVISO {b['id']}: validación de una rama ajena ({d['rama']}); se ignora")
                        self.s["sin_registro"][b["id"]] = 8
                        continue
                    reg = {"repo": repo, "build_id": b["id"], "digest": d["digest"], "start": b["start"], "end": b["end"],
                           "status": b["status"], "resultado": d["resultado"] if b["status"] == "SUCCEEDED" else "falla",
                           "detalle": d.get("detalle")}
                    v["validaciones"][modelo].append(reg)
                    self.log(f"validación {modelo} {v['rama']} {repo} {b['id'].split(':')[1][:8]} {b['status']} "
                             f"{reg['resultado']} digest {str(d['digest'])[7:19]} ({int(b['end'] - b['start'])} s)")

    # --- 2. eventos -----------------------------------------------------------------------------

    def build_unico(self, ev):
        for b in self.builds.get(proyecto("unico", ev["repo"]), []):
            if b["resolved"] == ev.get("commit"):
                return b
        return None

    def despachar(self):
        ahora = self.p.ahora()
        for ev in self.s["eventos"]:
            if ev["estado"] != "pendiente" or ahora - self.s["t0"] < ev["t"]:
                continue
            previos = [x for x in self.eventos_de(ev["n"], ev["repo"]) if x["i"] < ev["i"]]
            if any(x["estado"] != "unico_ok" for x in previos):
                self.esperar(f"evento-{ev['i']}", f"build único previo de c{self.c}-{ev['n']} {ev['repo']}")
                continue
            self.listo(f"evento-{ev['i']}")
            v = self.V(ev["n"])
            if ev["accion"] == "apertura":
                r = self.p.abrir(ev["n"], ev["repo"])
                v["app_pr"][ev["repo"]] = {"pr": r["pr"], "created_at": r["pr_created_at"]}
                if v["apertura"] is None or epoch(r["pr_created_at"]) < epoch(v["apertura"]):
                    v["apertura"] = r["pr_created_at"]
                self.log(f"abrir {v['rama']} {ev['repo']}: PR #{r['pr']} ({r['pr_created_at']}), commit {r['commit'][:7]}")
            else:
                r = self.p.actualizar(ev["n"], ev["repo"], ev["actualizacion"])
                self.log(f"actualizar {v['rama']} {ev['repo']} k={ev['actualizacion']}: commit {r['commit'][:7]}")
            ev.update(estado="despachado", commit=r["commit"], despachado=self.p.ahora(), retraso=int(self.p.ahora() - self.s["t0"] - ev["t"]))
            self.esperar(f"unico-{ev['i']}", f"build único de {v['rama']} {ev['repo']} ({r['commit'][:7]})")

    def seguir_unicos(self):
        for ev in self.s["eventos"]:
            if ev["estado"] != "despachado":
                continue
            b = self.build_unico(ev)
            if b is None or not b["complete"]:
                continue
            v = self.V(ev["n"])
            ev["unico"] = {"build_id": b["id"], "status": b["status"], "start": b["start"], "end": b["end"]}
            if b["status"] != "SUCCEEDED":
                ev["estado"] = "unico_falla"
                raise Abortar(f"el build único {b['id']} de {v['rama']} {ev['repo']} terminó {b['status']}")
            tag = self.p.tag_json(cd_de(ev["repo"]), v["rama"])
            if not tag or tag.get("commit") != ev["commit"]:
                continue  # el tag.json todavía no refleja este commit; se reintenta en la próxima vuelta
            ev["estado"] = "unico_ok"
            ev["digest"] = tag["digest"]
            self.listo(f"unico-{ev['i']}")
            ultimo = self.eventos_de(ev["n"], ev["repo"])[-1]
            if ultimo["i"] == ev["i"]:
                v["digest"][ev["repo"]] = tag["digest"]
            self.log(f"build único {b['id'].split(':')[1][:8]} {v['rama']} {ev['repo']} SUCCEEDED, tag {tag.get('tag', '')} "
                     f"digest {tag['digest'][7:19]}")

    # --- 4. cola del fijo -----------------------------------------------------------------------

    def cola_fijo(self):
        cola = self.s["cola_fijo"]
        en_curso = cola["en_curso"]
        if en_curso is not None:
            b = next((x for x in self.builds.get(proyecto("fijo", en_curso["repo"]), []) if x["resolved"] == en_curso["commit_dev"]), None)
            if b is None:
                return
            if en_curso.get("build_id") is None:
                en_curso["build_id"] = b["id"]
                self.log(f"fijo: build {b['id']} de la integración de {en_curso['rama']} {en_curso['repo']}")
            if not b["complete"]:
                return
            en_curso.update(build_status=b["status"], build_end=b["end"])
            self.log(f"fijo: build {b['id'].split(':')[1][:8]} terminó {b['status']} ({int(b['end'] - b['start'])} s)")
            self.listo("fijo-build")
            cola["en_curso"] = None
        if any(not b["complete"] for repo in ("backend", "frontend") for b in self.builds.get(proyecto("fijo", repo), [])):
            return
        try:
            prs = self.p.prs_dev()
        except ErrorEjecutor as error:
            self.aviso_consulta(error)
            return
        prs = [p for p in prs if any(v["rama"] == p["head"] for v in self.s["versiones"].values())]
        pr = elegir_pr_cola(prs, self.s["integrados"])
        if pr is None:
            return
        v = next(x for x in self.s["versiones"].values() if x["rama"] == pr["head"])
        r = self.p.integrar(pr["cd"], pr["head"], "dev", f"integracion {pr['head']} (PR #{pr['number']})")
        self.s["integrados"].setdefault(f"{pr['cd']}#{pr['number']}", []).append(pr["sha"])
        integ = {"version": f"c{self.c}-{v['n']}", "rama": pr["head"], "repo": pr["repo"], "cd": pr["cd"], "pr": pr["number"],
                 "pr_created_at": pr["created_at"], "head": pr["sha"], "commit_dev": r["commit"], "conflicto": r["conflicto"],
                 "hora": iso(self.p.ahora()), "build_id": None}
        cola["integraciones"].append(integ)
        cola["en_curso"] = integ
        self.s["merged_pendientes"].append({"repo_gh": pr["cd"], "pr": pr["number"], "base": "dev", "rama": pr["head"],
                                            "desde": self.p.ahora(), "integracion": len(cola["integraciones"]) - 1})
        self.esperar("fijo-build", f"build del fijo de {pr['head']} {pr['repo']}")
        self.log(f"fijo: integra {pr['head']} {pr['repo']} (PR #{pr['number']}, {pr['created_at']}) en dev -> "
                 f"{r['commit'][:7]} (conflicto: {r['conflicto']})")

    # --- 5. validación final --------------------------------------------------------------------

    def finales(self):
        for v in self.s["versiones"].values():
            evs = self.eventos_de(v["n"])
            if any(ev["estado"] != "unico_ok" for ev in evs):
                continue
            for modelo in MODELOS:
                if v["final"][modelo] is not None:
                    continue
                self.esperar(f"final-{v['n']}-{modelo}", f"validación final de {v['rama']} en {modelo}")
                f = validacion_final(v["repos"], v["digest"], v["validaciones"][modelo])
                if f is None:
                    continue
                f["tiempo_validacion_seg"] = round(f["end"] - epoch(v["apertura"]), 1)
                v["final"][modelo] = f
                self.listo(f"final-{v['n']}-{modelo}")
                self.log(f"VALIDACIÓN FINAL {v['rama']} {modelo}: {f['build_id']} {f['resultado']} fin {iso(f['end'])} "
                         f"tiempo de validación {f['tiempo_validacion_seg']:.0f} s")

    # --- 6. cierres -----------------------------------------------------------------------------

    def _build_cierre(self, repo, pr):
        return next((b for b in self.builds.get(proyecto("cierre", repo), []) if b["source"] == f"pr/{pr}"), None)

    def _build_por_id(self, repo, build_id):
        return next((b for b in self.builds.get(proyecto("cierre", repo), []) if b["id"] == build_id), None)

    def cierre_efimero(self, v):
        c = v["cierre"]["efimero"]
        if c["estado"] == "hecho" or v["final"]["efimero"] is None:
            return
        if c["estado"] == "pendiente":
            c["estado"] = "en_curso"
        integrada = v["resultado"] == "integrada"
        for repo in v["repos"]:
            r = c["repos"].setdefault(repo, {"fase": "pr"})
            if r["fase"] == "hecho":
                continue
            cd = cd_de(repo)
            if r["fase"] == "pr":
                pr = self.p.pr_crear(cd, "main-efimero", v["rama"], f"{v['rama']} cierre efimero ({v['resultado']})")
                r.update(pr=pr, t_accion=self.p.ahora())
                if integrada:
                    m = self.p.integrar(cd, v["rama"], "main-efimero", f"cierre {v['rama']} integrada (PR #{pr})")
                    r.update(accion_pr="merge", commit=m["commit"], conflicto=m["conflicto"])
                    self.s["merged_pendientes"].append({"repo_gh": cd, "pr": pr, "base": "main-efimero", "rama": v["rama"],
                                                        "desde": self.p.ahora()})
                else:
                    self.p.pr_cerrar(cd, pr)
                    r["accion_pr"] = "close"
                r.update(fase="webhook", t_accion=self.p.ahora())
                self.esperar(f"cierre-{v['n']}-{repo}", f"cierre efímero de {v['rama']} {repo}")
                self.log(f"cierre efímero {v['rama']} {repo}: PR #{pr} -> main-efimero, "
                         f"{'merge local y push' if integrada else 'cerrado sin merge'}")
                return
            if r["fase"] == "webhook":
                b = self._build_cierre(repo, r["pr"])
                d = esperar_webhook(r["t_accion"], self.p.ahora(), b)
                if d == "esperar":
                    return
                if d == "seguir":
                    r.update(fase="build", build_id=b["id"], via="webhook")
                    self.log(f"cierre efímero {v['rama']} {repo}: build del webhook {b['id']}")
                else:
                    bid = self.p.cierre_manual(repo, v["rama"])
                    r.update(fase="build", build_id=bid, via="manual", nota="webhook no reaccionó")
                    self.log(f"cierre efímero {v['rama']} {repo}: webhook no reaccionó en {WEBHOOK_SEG} s; cierre manual {bid}")
                return
            if r["fase"] == "build":
                b = self._build_por_id(repo, r["build_id"])
                if b is None or not b["complete"]:
                    return
                d = self.p.destruccion(b["id"])
                if d is None:
                    r["sin_registro"] = r.get("sin_registro", 0) + 1
                    if r["sin_registro"] < 8:
                        return
                    d = {"accion": None, "resultado": None}
                r.update(fase="hecho", status=b["status"], end=b["end"], accion=d["accion"], resultado=d["resultado"],
                         otra_rama_existe=d.get("otra_rama_existe"), cierre_resultado=d.get("cierre_resultado"))
                self.listo(f"cierre-{v['n']}-{repo}")
                self.log(f"cierre efímero {v['rama']} {repo}: {b['id'].split(':')[1][:8]} {b['status']}, accion {d['accion']}, "
                         f"resultado {d['resultado']} ({int(b['end'] - b['start'])} s)")
                if b["status"] != "SUCCEEDED":
                    raise Abortar(f"el cierre {b['id']} de {v['rama']} {repo} terminó {b['status']}")
                continue
        # todos los repos cerrados
        resultados = [c["repos"][r] for r in v["repos"]]
        if c["manual_extra"] is not None:
            m = c["manual_extra"]
            b = self._build_por_id(m["repo"], m["build_id"])
            if b is None or not b["complete"]:
                return
            d = self.p.destruccion(b["id"]) or {"accion": None, "resultado": None}
            m.update(status=b["status"], end=b["end"], accion=d["accion"], resultado=d["resultado"])
            resultados = resultados + [m]
            self.log(f"cierre efímero {v['rama']}: manual {b['id'].split(':')[1][:8]} {b['status']}, resultado {d['resultado']}")
        decision = decidir_cierre(resultados, v["repos"])
        if decision == "hecho":
            quien = next(r for r in resultados if r.get("accion") in ("destruir", "manual")
                         and r.get("resultado") in ("ok", "nada que destruir"))
            via = quien.get("via", "webhook")
            c.update(estado="hecho", destruccion={"via": via, "build_id": quien["build_id"], "end": quien["end"],
                                                  "resultado": quien["resultado"], "motivo_manual": quien.get("nota")})
            self.listo(f"cierre-{v['n']}-extra")
            self.log(f"CERRADA {v['rama']} en efímero: entorno destruido por {via} ({quien['build_id']})")
        elif decision == "manual" and c["manual_extra"] is None:
            bid = self.p.cierre_manual(v["repos"][0], v["rama"])
            c["manual_extra"] = {"repo": v["repos"][0], "build_id": bid, "via": "manual", "nota": "los dos cierres esperaron al otro -cd"}
            self.esperar(f"cierre-{v['n']}-extra", f"cierre manual de {v['rama']}")
            self.log(f"cierre efímero {v['rama']}: los dos cierres esperaron al otro -cd; cierre manual {bid}")
        elif decision == "falla" and (c["manual_extra"] is None or c["manual_extra"].get("end") is not None):
            raise Abortar(f"el cierre efímero de {v['rama']} no destruyó el entorno: {resultados}")

    def cierre_fijo(self, v):
        c = v["cierre"]["fijo"]
        if c["estado"] == "hecho" or v["final"]["fijo"] is None:
            return
        integrada = v["resultado"] == "integrada"
        for repo in v["repos"]:
            cd = cd_de(repo)
            pr = self.p.pr_crear(cd, "main-fijo", v["rama"], f"{v['rama']} cierre fijo ({v['resultado']})")
            r = {"pr": pr, "hora": iso(self.p.ahora())}
            if integrada:
                m = self.p.integrar(cd, v["rama"], "main-fijo", f"cierre {v['rama']} integrada (PR #{pr})")
                r.update(accion_pr="merge", commit=m["commit"], conflicto=m["conflicto"])
                self.s["merged_pendientes"].append({"repo_gh": cd, "pr": pr, "base": "main-fijo", "rama": v["rama"], "desde": self.p.ahora()})
            else:
                self.p.pr_cerrar(cd, pr)
                r["accion_pr"] = "close"
            c["repos"][repo] = r
            self.log(f"cierre fijo {v['rama']} {repo}: PR #{pr} -> main-fijo, {'merge local y push' if integrada else 'cerrado sin merge'}")
        c["estado"] = "hecho"
        self.log(f"CERRADA {v['rama']} en fijo")

    def cierres(self):
        for v in self.s["versiones"].values():
            self.cierre_fijo(v)
            self.cierre_efimero(v)

    # --- merged ---------------------------------------------------------------------------------

    def verificar_merged(self):
        quedan = []
        for m in self.s["merged_pendientes"]:
            e = self.p.pr_estado(m["repo_gh"], m["pr"])
            if e["merged"] or self.p.ahora() - m["desde"] >= MERGED_SEG:
                m.update(merged=e["merged"], state=e["state"], verificado=iso(self.p.ahora()))
                self.s.setdefault("merged", []).append(m)
                if "integracion" in m:
                    self.s["cola_fijo"]["integraciones"][m["integracion"]]["pr_merged"] = e["merged"]
                if not e["merged"]:
                    self.log(f"AVISO PR {m['repo_gh']}#{m['pr']} ({m['rama']} -> {m['base']}) no quedó marcado como merged ({e['state']})")
                else:
                    self.log(f"PR {m['repo_gh']}#{m['pr']} ({m['rama']} -> {m['base']}) marcado como merged")
            else:
                quedan.append(m)
        self.s["merged_pendientes"] = quedan

    # --- 7. limpieza ----------------------------------------------------------------------------

    def limpiezas(self):
        cierres_en_curso = any(not b["complete"] for repo in ("backend", "frontend") for b in self.builds.get(proyecto("cierre", repo), []))
        for v in self.s["versiones"].values():
            if v["limpieza"] is not None:
                continue
            if v["cierre"]["efimero"]["estado"] != "hecho" or v["cierre"]["fijo"]["estado"] != "hecho":
                continue
            if any(m["rama"] == v["rama"] for m in self.s["merged_pendientes"]):
                continue  # que se verifique el merged antes de borrar la rama
            if cierres_en_curso:
                self.esperar(f"limpieza-{v['n']}", f"limpieza de {v['rama']} (cierres en curso)")
                return
            self.listo(f"limpieza-{v['n']}")
            hecho = {"hora": iso(self.p.ahora()), "ramas_cd": [], "app": []}
            for repo in v["repos"]:
                self.p.borrar_rama(cd_de(repo), v["rama"])
                hecho["ramas_cd"].append(cd_de(repo))
            for repo in v["repos"]:
                pr = v["app_pr"][repo]["pr"]
                self.p.pr_cerrar(app_de(repo), pr)
                self.p.borrar_rama(app_de(repo), v["rama"])
                hecho["app"].append({"repo": app_de(repo), "pr": pr})
            v["limpieza"] = hecho
            self.log(f"limpieza {v['rama']}: ramas borradas en {', '.join(hecho['ramas_cd'])}; PR de la app cerrado y rama borrada")

    # --- esperas, fin ---------------------------------------------------------------------------

    def chequear_esperas(self):
        for clave, e in self.s["esperas"].items():
            if self.p.ahora() - e["desde"] > MAX_ESPERA_SEG:
                raise Abortar(f"espera de más de {MAX_ESPERA_SEG // 60} min: {e['que']} (desde {iso(e['desde'])})")

    def terminada(self):
        return all(v["limpieza"] is not None for v in self.s["versiones"].values()) and not self.s["merged_pendientes"]

    def resumen(self):
        versiones = []
        for v in self.s["versiones"].values():
            fila = {"n": v["n"], "rama": v["rama"], "tipo": v["tipo"], "repos": v["repos"], "resultado_escenario": v["resultado"],
                    "apertura": v["apertura"], "app_pr": v["app_pr"], "digest": v["digest"], "modelos": {}}
            for modelo in MODELOS:
                f = v["final"][modelo]
                m = {"validacion_final": None if f is None else {
                        "build_id": f["build_id"], "repo": f["repo"], "end": iso(f["end"]), "resultado": f["resultado"],
                        "por_repo": {r: dict(x, end=iso(x["end"])) for r, x in f["por_repo"].items()}},
                     "tiempo_validacion_seg": None if f is None else f["tiempo_validacion_seg"],
                     "validaciones": [dict(x, start=iso(x["start"]), end=iso(x["end"])) for x in v["validaciones"][modelo]]}
                if modelo == "fijo":
                    m["integraciones"] = [i for i in self.s["cola_fijo"]["integraciones"] if i["rama"] == v["rama"]]
                    m["cierre"] = v["cierre"]["fijo"]
                else:
                    m["cierre"] = v["cierre"]["efimero"]
                fila["modelos"][modelo] = m
            fila["limpieza"] = v["limpieza"]
            versiones.append(fila)
        return {"corrida": self.s["corrida"], "semilla": self.s["semilla"], "nivel": self.s["nivel"], "simulada": self.s["simulada"],
                "inicio": iso(self.s["inicio"]), "t0": iso(self.s["t0"]), "fin": iso(self.s["fin"]), "resultado": self.s["resultado"],
                "motivo": self.s["motivo"],
                "eventos": [{k: (iso(x) if k in ("despachado",) else x) for k, x in ev.items()} for ev in self.s["eventos"]],
                "versiones": versiones, "merged": self.s.get("merged", []), "integraciones_fijo": self.s["cola_fijo"]["integraciones"]}

    def escribir_corrida(self):
        if self.escribir:
            self.archivo_corrida.write_text(json.dumps(self.resumen(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def correr(self):
        self.s["inicio"] = self.p.ahora()
        try:
            if self.sin_reset:
                self.s["t0"] = self.p.ahora()
                self.log("sin reset: t = 0 ahora")
            else:
                self.log("reset.py" + (" (simulado: en real corre reset.py --corrida)" if self.p.simulada else ""))
                self.s["t0"] = self.p.reset()
            self.log(f"t = 0 en {iso(self.s['t0'])}; {len(self.s['eventos'])} eventos, {len(self.s['versiones'])} versiones")
            while True:
                inicio_vuelta = self.p.ahora()
                self.s["vueltas"] += 1
                try:
                    self.refrescar()
                    self.s["errores_consulta"] = 0
                except ErrorEjecutor as error:
                    self.aviso_consulta(error)
                    self.guardar()
                    self.p.dormir(VUELTA_SEG)
                    continue
                self.despachar()
                self.seguir_unicos()
                self.cola_fijo()
                self.finales()
                self.cierres()
                try:
                    self.verificar_merged()
                except ErrorEjecutor as error:
                    self.aviso_consulta(error)
                self.limpiezas()
                if self.terminada():
                    break
                self.chequear_esperas()
                self.guardar()
                self.p.dormir(max(0.0, VUELTA_SEG - (self.p.ahora() - inicio_vuelta)) if not self.p.simulada else VUELTA_SEG)
            self.s.update(fin=self.p.ahora(), resultado="ok")
            self.log(f"corrida c{self.c} terminada en {int(self.s['fin'] - self.s['t0'])} s desde t = 0")
            return 0
        except (Abortar, ErrorEjecutor, acciones.ErrorAccion, reset_mod.ErrorReset) as error:
            self.s.update(fin=self.p.ahora(), resultado="invalida", motivo=str(error))
            self.log(f"CORRIDA ABORTADA (inválida): {error}")
            return 2
        except Exception as error:  # noqa: BLE001 - un error inesperado también deja estado y motivo
            import traceback
            self.s.update(fin=self.p.ahora(), resultado="invalida", motivo=f"error inesperado: {type(error).__name__}: {error}")
            self.log(f"CORRIDA ABORTADA (error inesperado): {type(error).__name__}: {error}")
            for linea in traceback.format_exc().splitlines():
                self.log("  " + linea)
            return 3
        finally:
            self.guardar()
            self.escribir_corrida()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ejecuta una corrida completa en los dos modelos")
    parser.add_argument("--escenario", required=True)
    parser.add_argument("--sin-reset", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    escenario = json.loads(Path(args.escenario).read_text(encoding="utf-8"))
    if args.dry_run:
        ej = Ejecutor(escenario, PlataformaSimulada(escenario), sin_reset=args.sin_reset, escribir=False)
        codigo = ej.correr()
        print(json.dumps({v["rama"]: {m: v["modelos"][m]["tiempo_validacion_seg"] for m in MODELOS} | {
            "cierre_efimero": (v["modelos"]["efimero"]["cierre"].get("destruccion") or {}).get("via")}
            for v in ej.resumen()["versiones"]}, ensure_ascii=False))
        return codigo
    return Ejecutor(escenario, PlataformaReal(escenario), sin_reset=args.sin_reset).correr()


if __name__ == "__main__":
    sys.exit(main())
