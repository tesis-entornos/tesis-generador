#!/usr/bin/env python3
"""Ejecutor de una corrida completa en los dos modelos (fijo y efímero), con DOS RAMAS por versión y repo.

    python ejecutor.py --escenario escenarios/c<N>.json [--sin-reset] [--dry-run]

Diseño "dos ramas" (protocolo v4, esquema 2 del registro). Cada versión y repo tiene una rama de la app por modelo,
release/efimero/c<N>-<n> y release/fijo/c<N>-<n>, cada una con su PR a main y su build en la compartida. El mismo
contenido en las dos; el modelo va en el mensaje del commit, así el SHA y la imagen son distintos. El build de la
rama del efímero escribe tag.json (modelo = efimero) en release/c<N>-<n> del -cd, cuyo push dispara el efímero. El
de la rama del fijo lo escribe (modelo = fijo) en release/fijo/c<N>-<n>, que no dispara nada: el ejecutor integra
en dev el commit de ESA actualización (no la punta), de a una, por orden de entrega (fin del build).

Regla de actualización: la actualización k de una versión se hace en la rama de un modelo recién cuando terminaron
todas las validaciones en curso de esa versión en ese modelo (backend y frontend; sin builds ni entregas
pendientes) y el evento anterior de la cadena ya se validó ahí, más la brecha b_k del escenario (t_k - t del
evento anterior de la cadena, la misma para los dos modelos, sin re-sortear): T_m(k) = F_m + b_k.

Un bucle de un solo hilo que cada 15 s:
  1. (al inicio) corre reset.py salvo --sin-reset; su fin es t = 0;
  2. despacha por modelo: aperturas en t del escenario (los dos modelos, en orden alternado por n),
     actualizaciones en T_m(k); un evento de un modelo espera al build del evento anterior de la misma versión,
     repo y modelo;
  3. consulta builds (compartida, fijo, efímero, cierre), registros (validacion.json, destruccion.json) y, por
     cada build terminado de la compartida, el tag.json del commit que escribió en el -cd;
  4. cola del fijo: con el fijo libre, integra en dev el commit del -cd de la solicitud entregada primero y
     espera su build del fijo;
  5. validación final por modelo: para cada repo, la validación del digest del último evento de ese modelo;
  6. cierre en cada modelo (efímero: PR release/c<N>-<n> -> main-efimero; fijo: PR release/fijo/c<N>-<n> ->
     main-fijo), limpieza y registro final en registros/corrida-c<N>.json (esquema 2).

Si algo espera más de MAX_ESPERA_SEG (12 h), la corrida se aborta como inválida. --dry-run recorre el escenario contra
una plataforma simulada con reloj virtual, sin tocar nada.
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
import cierre_corrida
import depurador as depurador_mod
import protocolo as protocolo_mod
import reset as reset_mod

RAIZ = Path(__file__).resolve().parent
REGISTROS = RAIZ / "registros"
TRABAJO = RAIZ / ".trabajo"
ORG = "tesis-entornos"
REGION = "us-east-2"

VUELTA_SEG = 15
MAX_ESPERA_SEG = 720 * 60
WEBHOOK_SEG = 120
MERGED_SEG = 180
EXPIRACION_SEG = 4 * 24 * 3600
ENTREGA_EFIMERO_SEG = 600
ESQUEMA = 2
REGLA_ACTUALIZACION = ("dos ramas por modelo; la actualización k se hace en la rama de cada modelo en F_m + b_k, con "
                       "F_m = fin de la última validación de la versión en ese modelo cuando no queda ninguna "
                       "validación, build ni entrega en curso de la versión en ese modelo (backend y frontend) y el "
                       "evento anterior de la cadena ya se validó ahí, y b_k = t_k - t del evento anterior de la "
                       "cadena del escenario (la misma para los dos modelos); el fijo integra el commit k, de a uno, "
                       "por orden de entrega")
MODELOS = ("efimero", "fijo")

PERFILES = {"tesis-build-": "tf-compartida", "tesis-efimero-": "tf-efimero", "tesis-fijo-": "tf-fijo"}
BUCKET_REGISTROS = {"efimero": ("tf-efimero", "tesis-efimero-registros-398846004421"),
                    "fijo": ("tf-fijo", "tesis-fijo-registros-491265021424")}
ECR_REGISTRO = "582621604637"


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


def rama_cd(rama, modelo):
    """Rama del -cd donde escribe el build de ese modelo: release/c<N>-<n> (efímero; la escucha su webhook) o
    release/fijo/c<N>-<n> (fijo; no dispara nada)."""
    return rama if modelo == "efimero" else acciones.rama_app(rama, "fijo")


# --------------------------------------------------------------------------------------------------
# Decisiones (puras)


def elegir_solicitud(solicitudes: list):
    """Solicitud del fijo entregada primero (listo; ante empate, el evento anterior) que todavía no se integró."""
    candidatas = [s for s in solicitudes if s.get("estado") == "pendiente"]
    if not candidatas:
        return None
    return min(candidatas, key=lambda s: (epoch(s["listo"]), s["ev"]))


def brechas(eventos: list) -> dict:
    """{i: (i del evento anterior de la cadena, brecha)} de las actualizaciones. La cadena de una versión es su
    apertura (la primera, del primer repo) y sus actualizaciones en orden; la segunda apertura de una combinación no
    la corta. eventos = eventos_ordenados con su índice i."""
    out = {}
    por_v = {}
    for ev in eventos:
        por_v.setdefault(ev["n"], []).append(ev)
    for evs in por_v.values():
        ant = min((e for e in evs if e["accion"] == "apertura"), key=lambda e: (e["t"], e["i"]))
        for e in sorted((e for e in evs if e["accion"] == "actualizacion"), key=lambda e: e["i"]):
            if e["t"] < ant["t"]:
                raise ValueError(f"c?-{e['n']}: la actualización {e['i']} es anterior a su evento previo de la cadena")
            out[e["i"]] = (ant["i"], e["t"] - ant["t"])
            ant = e
    return out


def estado_agregado(estados: list) -> str:
    """Estado de un evento a partir del de cada modelo."""
    s = set(estados)
    if s == {"unico_ok"}:
        return "unico_ok"
    if "despachado" in s:
        return "despachado"
    if s == {"cancelado"}:
        return "cancelado"
    if "pendiente" in s:
        return "pendiente"
    return "parcial"  # unico_ok en un modelo y cancelado en el otro (expiración)


def elegir_build_unico(builds: list):
    """builds del proyecto de la compartida con resolved == commit. ('esperar' | 'falla' | 'ok', escritor,
    duplicados): el escritor es el primer SUCCEEDED (por start); el resto son duplicados del webhook."""
    if not builds:
        return "esperar", None, []
    ok = sorted((b for b in builds if b["complete"] and b["status"] == "SUCCEEDED"), key=lambda b: (b["start"], b["id"]))
    if not ok:
        return ("falla" if all(b["complete"] for b in builds) else "esperar"), None, []
    return "ok", ok[0], [b for b in builds if b["id"] != ok[0]["id"]]


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


def ultimo_evento(eventos: list, n: int):
    """Hora del último evento despachado de la versión n (apertura o actualización de cualquiera de sus repos)."""
    horas = [ev["despachado"] for ev in eventos if ev["n"] == n and ev.get("despachado") is not None]
    return max(horas) if horas else None


def versiones_que_expiran(versiones: list, eventos: list, ahora: float, umbral: float) -> list:
    """n de las versiones abiertas cuyo último evento propio es más viejo que el umbral y que siguen sin cerrar
    (cierre pendiente) en algún modelo."""
    vencen = []
    for v in versiones:
        if v.get("expirada"):
            continue
        if not any(v["cierre"][m]["estado"] == "pendiente" for m in MODELOS):
            continue
        ultimo = ultimo_evento(eventos, v["n"])
        if ultimo is not None and ahora - ultimo > umbral:
            vencen.append(v["n"])
    return vencen


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

    def abrir(self, n, repo, modelo):
        r, _ = acciones.abrir(self.escenario, n, repo, False, modelo=modelo, tag_libre=lambda sha: self.tag_libre(repo, sha))
        return {"commit": r["commit"], "arbol": r["arbol"], "pr": r["pr"], "pr_created_at": r["pr_created_at"]}

    def actualizar(self, n, repo, k, modelo):
        r, _ = acciones.actualizar(self.escenario, n, repo, k, False, modelo=modelo,
                                   tag_libre=lambda sha: self.tag_libre(repo, sha))
        return {"commit": r["commit"], "arbol": r["arbol"]}

    def _aws(self, perfil, *args):
        out = _correr(["aws", *args, "--profile", perfil, "--region", REGION, "--output", "json"]).stdout
        return json.loads(out) if out.strip() else None

    def builds(self, proy, desde):
        perfil = next(p for pre, p in PERFILES.items() if proy.startswith(pre))
        # Con dos builds por evento, tesis-build-<repo> puede pasar de 30 builds por corrida: se miran los 100 últimos.
        ids = self._aws(perfil, "codebuild", "list-builds-for-project", "--project-name", proy, "--query", "ids[0:100]") or []
        pedir = [i for i in ids if not (i in self._cache and self._cache[i]["complete"])]
        for j in range(0, len(pedir), 100):
            for b in self._aws(perfil, "codebuild", "batch-get-builds", "--ids", *pedir[j:j + 100], "--query", "builds") or []:
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

    def tag_json(self, cd, ref):
        """tag.json del -cd en una rama o en un commit."""
        r = _correr(["gh", "api", f"repos/{ORG}/{cd}/contents/tag.json?ref={ref}"], check=False)
        if r.returncode != 0:
            return None
        return json.loads(base64.b64decode(json.loads(r.stdout)["content"]))

    def integrar(self, cd, ref, base, mensaje):
        """merge --no-ff de ref (origin/<rama> o un SHA del -cd) sobre base y push."""
        clon = acciones.preparar_clon(cd, TRABAJO, f"https://github.com/{ORG}/{cd}.git")
        _correr(["git", "merge", "--abort"], cwd=clon, check=False)
        _correr(["git", "checkout", "--quiet", "-f", "-B", base, f"origin/{base}"], cwd=clon)
        conflicto = merge_con_tag(clon, ref, mensaje)
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

    def ref_sha(self, repo_gh, rama):
        r = _correr(["gh", "api", f"repos/{ORG}/{repo_gh}/git/ref/heads/{rama}", "-q", ".object.sha"], check=False)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    def borrar_rama(self, repo_gh, rama):
        r = _correr(["gh", "api", "-X", "DELETE", f"repos/{ORG}/{repo_gh}/git/refs/heads/{rama}"], check=False)
        if r.returncode != 0 and "Reference does not exist" not in (r.stdout + r.stderr) and "Not Found" not in (r.stdout + r.stderr):
            raise ErrorEjecutor(f"no se pudo borrar {repo_gh} {rama}: {(r.stderr or r.stdout).strip()[:300]}")
        return r.returncode == 0

    # --- controles de dos ramas (solo lectura) ---

    def tag_libre(self, repo, sha):
        """True si el tag sha[:7] todavía no existe en el ECR de la compartida."""
        r = _correr(["aws", "ecr", "describe-images", "--registry-id", ECR_REGISTRO, "--repository-name", app_de(repo),
                     "--image-ids", f"imageTag={sha[:7]}", "--profile", "tf-compartida", "--region", REGION, "--output", "json"],
                    check=False)
        if r.returncode == 0:
            return False
        if "ImageNotFoundException" in (r.stderr + r.stdout):
            return True
        raise ErrorEjecutor(f"no se pudo consultar el tag {sha[:7]} en ECR: {reset_mod.redactar((r.stderr or r.stdout).strip())[:300]}")

    def imagen(self, repo, digest):
        """Tags y hora de push de la imagen de un digest en el ECR de la compartida."""
        d = self._aws("tf-compartida", "ecr", "describe-images", "--registry-id", ECR_REGISTRO, "--repository-name", app_de(repo),
                      "--image-ids", f"imageDigest={digest}", "--query", "imageDetails[0]")
        return {"tags": sorted((d or {}).get("imageTags") or []), "pushed_at": epoch((d or {}).get("imagePushedAt"))}

    def historial_cd(self, cd, rama):
        """Commits de la rama del -cd que no están en main, con lo que dice su tag.json."""
        r = _correr(["gh", "api", f"repos/{ORG}/{cd}/compare/main...{rama}", "-q", "[.commits[].sha]"], check=False)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        out = []
        for sha in json.loads(r.stdout):
            t = self.tag_json(cd, sha) or {}
            out.append({"sha": sha, "modelo": t.get("modelo"), "commit": t.get("commit"), "digest": t.get("digest"), "rama": t.get("rama")})
        return out


# --------------------------------------------------------------------------------------------------
# Plataforma simulada (dry-run y pruebas): reloj virtual, duraciones típicas medidas en las pruebas reales


DUR = {"unico": 90, "efimero_nuevo": 640, "efimero": 330, "fijo_backend": 330, "fijo_frontend": 141,
       "webhook": 20, "esperar_otro": 25, "destruir": 360}


class PlataformaSimulada:
    """Dos ramas: el build de release/efimero/* escribe en release/c<N>-<n> del -cd y dispara el efímero (serializado
    por rama, como esperar-turno.sh); el de release/fijo/* escribe en release/fijo/c<N>-<n> y abre el PR -> dev, sin
    disparar nada. Los builds se ven desde que se piden (como en CodeBuild, donde la espera del turno es parte del
    build); start es cuando arrancan de verdad."""
    simulada = True

    def __init__(self, escenario, inicio=None):
        self.escenario = escenario
        self.corrida = escenario["corrida"]
        self.t = inicio if inicio is not None else float(int(time.time()))
        self._cola, self._seq = [], 0
        self.b = {}           # proyecto -> [build]
        self.val, self.des = {}, {}
        self.prs = {}         # repo_gh -> [pr]
        self.tags = {}        # (cd, rama o sha) -> tag
        self.ramas = set()    # (repo_gh, rama)
        self.entornos = set()
        self.fin_rama = {}    # rama -> fin del último despliegue efímero (serialización)
        self.n = 0
        self.refs = {}        # (cd, rama) -> sha de la punta
        self.hist = {}        # (cd, rama) -> [sha]
        self.imagenes = {}    # digest -> {tags, pushed_at}

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
             "resolved": resolved, "source": source, "pedido": self.t}
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

    @staticmethod
    def _arbol(n, repo, k):
        return hashlib.sha1(f"arbol|{n}|{repo}|{k}".encode()).hexdigest()  # el mismo en los dos modelos

    def _evento_app(self, n, repo, modelo, k, pr):
        v = acciones.version(self.escenario, n)
        rama, cd = v["rama"], cd_de(repo)
        destino = rama_cd(rama, modelo)
        commit = self._h("app", n, repo, modelo, k)

        def tras_unico(b):
            digest = "sha256:" + hashlib.sha256(commit.encode()).hexdigest()
            tag = {"repo": f"{ECR_REGISTRO}.dkr.ecr.{REGION}.amazonaws.com/{app_de(repo)}", "commit": commit,
                   "tag": commit[:7], "digest": digest, "rama": rama, "modelo": modelo}
            self.imagenes[digest] = {"tags": [commit[:7]], "pushed_at": b["end"] - 10}
            cd_sha = self._h("cd", destino)
            self.tags[(cd, destino)] = tag
            self.tags[(cd, cd_sha)] = tag
            self.ramas.add((cd, destino))
            self.refs[(cd, destino)] = cd_sha
            self.hist.setdefault((cd, destino), []).append(cd_sha)
            if modelo == "efimero":
                inicio = max(self.t + 1, self.fin_rama.get(rama, 0))
                dur = DUR["efimero"] if rama in self.entornos else DUR["efimero_nuevo"]
                self.entornos.add(rama)
                self.fin_rama[rama] = inicio + dur
                e = self._build(proyecto("efimero", repo), inicio, dur, cd_sha, cd_sha)
                self.val[e["id"]] = {"rama": rama, "digest": digest, "resultado": "ok", "detalle": "ok"}
            else:
                abierto = [p for p in self.prs.get(cd, []) if p["head"] == destino and p["base"] == "dev" and p["state"] == "OPEN"]
                if abierto:
                    abierto[0]["sha"] = cd_sha
                else:
                    self._pr(cd, "dev", destino, cd_sha)
        self._build(proyecto("unico", repo), self.t + 2, DUR["unico"], commit, f"pr/{pr}", tras_unico)
        return commit

    def abrir(self, n, repo, modelo):
        rama = acciones.rama_app(acciones.version(self.escenario, n)["rama"], modelo)
        p = self._pr(app_de(repo), "main", rama)
        self.ramas.add((app_de(repo), rama))
        commit = self._evento_app(n, repo, modelo, 0, p["number"])
        return {"commit": commit, "arbol": self._arbol(n, repo, 0), "pr": p["number"], "pr_created_at": iso(self.t)}

    def actualizar(self, n, repo, k, modelo):
        rama = acciones.rama_app(acciones.version(self.escenario, n)["rama"], modelo)
        p = next(x for x in self.prs[app_de(repo)] if x["head"] == rama)
        return {"commit": self._evento_app(n, repo, modelo, k, p["number"]), "arbol": self._arbol(n, repo, k)}

    def builds(self, proy, desde):
        return [dict(b) for b in self.b.get(proy, []) if b["pedido"] >= desde - 1 and b["pedido"] <= self.t]

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

    def tag_json(self, cd, ref):
        return self.tags.get((cd, ref))

    def _cierre_webhook(self, cd, rama, pr, evento):
        repo = cd.split("-")[1]
        otro = cd_de("frontend" if repo == "backend" else "backend")
        otra = (otro, rama) in self.ramas
        accion = "esperar_otro" if otra else "destruir"
        b = self._build(proyecto("cierre", repo), self.t + DUR["webhook"], DUR[accion], None, f"pr/{pr}")
        self.des[b["id"]] = {"accion": accion, "resultado": "esperando el cierre del otro -cd" if otra else "ok",
                             "otra_rama_existe": otra, "evento": evento, "rama": rama}

    def integrar(self, cd, ref, base, mensaje):
        commit = self._h("merge", cd, base)
        if base == "dev":
            repo = cd.split("-")[1]
            tag = self.tags[(cd, ref)]
            f = self._build(proyecto("fijo", repo), self.t + 3, DUR[f"fijo_{repo}"], commit, commit)
            self.val[f["id"]] = {"rama": tag["rama"], "digest": tag["digest"], "resultado": "ok", "detalle": "ok"}
            return {"commit": commit, "conflicto": "tag.json"}
        rama = ref[len("origin/"):] if ref.startswith("origin/") else ref
        for p in self.prs.get(cd, []):
            if p["head"] == rama and p["base"] == base and p["state"] == "OPEN":
                p["state"] = "MERGED"
                if base == "main-efimero":
                    self._cierre_webhook(cd, rama, p["number"], "PULL_REQUEST_MERGED")
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

    def ref_sha(self, repo_gh, rama):
        return self.refs.get((repo_gh, rama)) if (repo_gh, rama) in self.ramas else None

    def borrar_rama(self, repo_gh, rama):
        self.ramas.discard((repo_gh, rama))
        return True

    def tag_libre(self, repo, sha):
        return True

    def imagen(self, repo, digest):
        return dict(self.imagenes.get(digest) or {"tags": [], "pushed_at": None})

    def historial_cd(self, cd, rama):
        out = []
        for sha in self.hist.get((cd, rama), []):
            t = self.tags.get((cd, sha)) or {}
            out.append({"sha": sha, "modelo": t.get("modelo"), "commit": t.get("commit"), "digest": t.get("digest"), "rama": t.get("rama")})
        return out


# --------------------------------------------------------------------------------------------------
# Ejecutor


def nuevo_modelo():
    return {"estado": "pendiente", "T": None, "F": None, "commit": None, "arbol": None, "pr": None, "despachado": None,
            "retraso": None, "unico": None, "builds": [], "duplicados": [], "digest": None, "tag": None, "cd_sha": None,
            "imagen": None, "entregado": None, "efimero_build": None}


class Ejecutor:
    def __init__(self, escenario, plataforma, sin_reset=False, escribir=True, expiracion_seg=EXPIRACION_SEG, verificar=True):
        self.e = escenario
        self.verificar = verificar
        self.expiracion_seg = expiracion_seg
        self.p = plataforma
        self.c = escenario["corrida"]
        self.sin_reset = sin_reset
        self.escribir = escribir
        self.archivo_estado = REGISTROS / f"estado-c{self.c}.json"
        self.archivo_log = REGISTROS / f"c{self.c}.log"
        self.archivo_corrida = REGISTROS / f"corrida-c{self.c}.json"
        eventos = [dict(ev, i=i, estado="pendiente", despachado=None, m={m: nuevo_modelo() for m in MODELOS})
                   for i, ev in enumerate(escenario["eventos_ordenados"])]
        for i, (previo, b) in brechas(eventos).items():
            eventos[i].update(previo=previo, brecha=b)
        self.s = {
            "corrida": f"c{self.c}", "semilla": escenario["semilla"], "nivel": escenario["nivel"], "esquema": ESQUEMA,
            "simulada": plataforma.simulada, "inicio": None, "t0": None, "fin": None, "resultado": None, "motivo": None,
            "eventos": eventos,
            "versiones": {},
            "cola_fijo": {"en_curso": None, "integraciones": []},
            "solicitudes_fijo": [],
            "merged_pendientes": [],
            "esperas": {},
            "avisos": [],
            "vueltas": 0,
            "expiracion_seg": expiracion_seg,
            "depurador": None,
            "cierre": None,
            "protocolo": None,
        }
        for v in escenario["versiones"]:
            self.s["versiones"][str(v["n"])] = {
                "n": v["n"], "rama": v["rama"], "tipo": v["tipo"], "repos": v["repos"], "resultado": v["resultado"],
                "app_pr": {m: {} for m in MODELOS}, "apertura": None, "apertura_m": {m: None for m in MODELOS},
                "digest_m": {m: {} for m in MODELOS}, "validaciones": {"efimero": [], "fijo": []},
                "final": {"efimero": None, "fijo": None},
                "cierre": {"efimero": {"estado": "pendiente", "repos": {}, "manual_extra": None, "destruccion": None},
                           "fijo": {"estado": "pendiente", "repos": {}}},
                "limpieza": None, "historial_cd": {},
                "expirada": None, "resultado_cierre": None, "cd_commits": [],
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

    def aviso(self, texto):
        if texto not in self.s["avisos"]:
            self.s["avisos"].append(texto)
            self.log(f"AVISO {texto}")

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

    def agregar(self, ev):
        ev["estado"] = estado_agregado([ev["m"][m]["estado"] for m in MODELOS])
        ds = [ev["m"][m]["despachado"] for m in MODELOS if ev["m"][m]["despachado"] is not None]
        ev["despachado"] = max(ds) if ds else None

    def digests_modelo(self, v, m):
        return {ev["m"][m]["digest"] for ev in self.eventos_de(v["n"]) if ev["m"][m]["digest"]}

    def validado(self, ev, m):
        """Fin de la (primera) validación terminada del digest del evento en el modelo, o None."""
        d = ev["m"][m]["digest"]
        if d is None:
            return None
        ends = [x["end"] for x in self.V(ev["n"])["validaciones"][m] if x["digest"] == d and x["end"] is not None]
        return min(ends) if ends else None

    # --- consultas ------------------------------------------------------------------------------

    def refrescar(self):
        desde = self.s["t0"]
        hay_cierres = any(v["cierre"]["efimero"]["estado"] not in ("pendiente",) for v in self.s["versiones"].values())
        for repo in ("backend", "frontend"):
            for tipo in ("unico", "efimero", "fijo") + (("cierre",) if hay_cierres else ()):
                self.builds[proyecto(tipo, repo)] = self.p.builds(proyecto(tipo, repo), desde)
        # builds de la compartida de cada commit (para detectar duplicados del webhook)
        for ev in self.s["eventos"]:
            for m in MODELOS:
                x = ev["m"][m]
                if x["commit"]:
                    x["builds"] = [{"id": b["id"], "status": b["status"], "complete": b["complete"], "start": b["start"],
                                    "end": b["end"]} for b in self.builds.get(proyecto("unico", ev["repo"]), [])
                                   if b["resolved"] == x["commit"]] or x["builds"]
        # validaciones terminadas de los dos modelos
        for modelo in MODELOS:
            otro = "fijo" if modelo == "efimero" else "efimero"
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
                    if d["digest"] in self.digests_modelo(v, otro):
                        raise Abortar(f"entrega al modelo equivocado: la validación {b['id']} del {modelo} desplegó la imagen "
                                      f"{str(d['digest'])[:19]} de la rama del {otro} ({v['rama']} {repo})")
                    reg = {"repo": repo, "build_id": b["id"], "digest": d["digest"], "start": b["start"], "end": b["end"],
                           "status": b["status"], "resultado": d["resultado"] if b["status"] == "SUCCEEDED" else "falla",
                           "detalle": d.get("detalle")}
                    v["validaciones"][modelo].append(reg)
                    self.log(f"validación {modelo} {v['rama']} {repo} {b['id'].split(':')[1][:8]} {b['status']} "
                             f"{reg['resultado']} digest {str(d['digest'])[7:19]} ({int(b['end'] - b['start'])} s)")

    # --- 2. eventos -----------------------------------------------------------------------------

    def calcular_T(self):
        """T_m(k) = F_m + b_k de las actualizaciones cuyo F_m ya se puede fijar en cada modelo."""
        for ev in self.s["eventos"]:
            if ev["accion"] != "actualizacion":
                continue
            for m in MODELOS:
                x = ev["m"][m]
                if x["T"] is not None or x["estado"] != "pendiente":
                    continue
                prev = self.s["eventos"][ev["previo"]]
                if prev["m"][m]["entregado"] is None or self.validado(prev, m) is None:
                    continue
                otros = [e for e in self.eventos_de(ev["n"]) if e["i"] != ev["i"]]
                if any(e["m"][m]["estado"] == "despachado" or (e["m"][m]["estado"] == "unico_ok" and e["m"][m]["entregado"] is None)
                       for e in otros):
                    continue  # build o entrega pendiente de la versión en m
                if any(e["m"][m]["entregado"] is not None and self.validado(e, m) is None for e in otros):
                    continue  # validación en curso de la versión en m
                x["F"] = max(self.validado(e, m) for e in otros if e["m"][m]["entregado"] is not None)
                x["T"] = x["F"] + ev["brecha"]
                self.log(f"{self.V(ev['n'])['rama']} {ev['repo']} k={ev['actualizacion']} en {m}: validaciones previas terminadas "
                         f"en {iso(x['F'])}, brecha {ev['brecha']} s -> T = {iso(x['T'])}")

    def despachar(self):
        self.calcular_T()
        ahora = self.p.ahora()
        for ev in self.s["eventos"]:
            v = self.V(ev["n"])
            orden = MODELOS if ev["n"] % 2 == 1 else tuple(reversed(MODELOS))
            for m in orden:
                x = ev["m"][m]
                if x["estado"] != "pendiente":
                    continue
                if ev["accion"] == "apertura":
                    if ahora - self.s["t0"] < ev["t"]:
                        continue
                    objetivo = self.s["t0"] + ev["t"]
                elif x["T"] is None or ahora < x["T"]:
                    continue
                else:
                    objetivo = x["T"]
                previos = [e for e in self.eventos_de(ev["n"], ev["repo"]) if e["i"] < ev["i"]]
                if any(e["m"][m]["estado"] != "unico_ok" for e in previos):
                    self.esperar(f"evento-{ev['i']}-{m}", f"build previo de c{self.c}-{ev['n']} {ev['repo']} en {m}")
                    continue
                self.listo(f"evento-{ev['i']}-{m}")
                if ev["accion"] == "apertura":
                    r = self.p.abrir(ev["n"], ev["repo"], m)
                    v["app_pr"][m][ev["repo"]] = {"pr": r["pr"], "created_at": r["pr_created_at"]}
                    if v["apertura_m"][m] is None or epoch(r["pr_created_at"]) < epoch(v["apertura_m"][m]):
                        v["apertura_m"][m] = r["pr_created_at"]
                    if v["apertura"] is None or epoch(r["pr_created_at"]) < epoch(v["apertura"]):
                        v["apertura"] = r["pr_created_at"]
                    x["pr"] = r["pr"]
                    self.log(f"abrir {acciones.rama_app(v['rama'], m)} {ev['repo']}: PR #{r['pr']} ({r['pr_created_at']}), "
                             f"commit {r['commit'][:7]}")
                else:
                    r = self.p.actualizar(ev["n"], ev["repo"], ev["actualizacion"], m)
                    self.log(f"actualizar {acciones.rama_app(v['rama'], m)} {ev['repo']} k={ev['actualizacion']}: commit {r['commit'][:7]}")
                x.update(estado="despachado", commit=r["commit"], arbol=r.get("arbol"), despachado=self.p.ahora(),
                         retraso=round(self.p.ahora() - objetivo, 1))
                self.esperar(f"unico-{ev['i']}-{m}", f"build de {acciones.rama_app(v['rama'], m)} {ev['repo']} ({r['commit'][:7]})")
            self.agregar(ev)

    def seguir_unicos(self):
        for ev in self.s["eventos"]:
            for m in MODELOS:
                x = ev["m"][m]
                if x["estado"] != "despachado":
                    continue
                v = self.V(ev["n"])
                bs = [b for b in self.builds.get(proyecto("unico", ev["repo"]), []) if b["resolved"] == x["commit"]]
                decision, escritor, duplicados = elegir_build_unico(bs)
                if decision == "esperar":
                    continue
                if decision == "falla":
                    x["estado"] = "unico_falla"
                    x["unico"] = {"build_id": bs[0]["id"], "status": bs[0]["status"], "start": bs[0]["start"], "end": bs[0]["end"]}
                    self.agregar(ev)
                    raise Abortar(f"el build {bs[0]['id']} de {acciones.rama_app(v['rama'], m)} {ev['repo']} terminó {bs[0]['status']}")
                cd, destino = cd_de(ev["repo"]), rama_cd(v["rama"], m)
                sha = self.p.ref_sha(cd, destino)
                tag = self.p.tag_json(cd, sha) if sha else None
                if not tag or tag.get("commit") != x["commit"]:
                    continue  # el -cd todavía no refleja este commit; se reintenta en la próxima vuelta
                if tag.get("modelo") != m:
                    raise Abortar(f"entrega al modelo equivocado: {cd} {destino} tiene tag.json con modelo {tag.get('modelo')!r} "
                                  f"para {acciones.rama_app(v['rama'], m)}")
                x.update(estado="unico_ok", digest=tag["digest"], tag=tag, cd_sha=sha, entregado=escritor["end"],
                         unico={"build_id": escritor["id"], "status": escritor["status"], "start": escritor["start"],
                                "end": escritor["end"]},
                         duplicados=[{"build_id": b["id"], "status": b["status"]} for b in duplicados])
                if duplicados:
                    self.aviso(f"builds duplicados del webhook para {acciones.rama_app(v['rama'], m)} {ev['repo']} "
                               f"{x['commit'][:7]}: {', '.join(b['id'] for b in duplicados)}")
                try:
                    x["imagen"] = self.p.imagen(ev["repo"], tag["digest"])
                except ErrorEjecutor as error:
                    self.aviso_consulta(error)
                self.listo(f"unico-{ev['i']}-{m}")
                if self.eventos_de(ev["n"], ev["repo"])[-1]["i"] == ev["i"]:
                    v["digest_m"][m][ev["repo"]] = tag["digest"]
                if m == "efimero":
                    if sha not in v["cd_commits"]:
                        v["cd_commits"].append(sha)
                    self.esperar(f"entrega-{ev['i']}-efimero", f"despliegue efímero de {v['rama']} {ev['repo']} ({sha[:7]})")
                else:
                    pr = next((p for p in self.p.prs_dev() if p["cd"] == cd and p["head"] == destino), None)
                    self.s["solicitudes_fijo"].append({
                        "ev": ev["i"], "n": ev["n"], "repo": ev["repo"], "k": ev["actualizacion"], "cd": cd, "rama": v["rama"],
                        "rama_cd": destino, "cd_sha": sha, "digest": tag["digest"], "listo": iso(escritor["end"]),
                        "pr_dev": pr["number"] if pr else None, "estado": "pendiente"})
                self.log(f"build {escritor['id'].split(':')[1][:8]} {acciones.rama_app(v['rama'], m)} {ev['repo']} SUCCEEDED, "
                         f"tag {tag.get('tag', '')} digest {tag['digest'][7:19]} -> {cd} {destino} {sha[:7]}"
                         + (" (entrega al efímero)" if m == "efimero" else " (solicitud en la cola del fijo)"))
            self.agregar(ev)

    def seguir_entregas_efimero(self):
        """El push del build a release/c<N>-<n> del -cd tiene que disparar el despliegue del efímero."""
        for ev in self.s["eventos"]:
            x = ev["m"]["efimero"]
            if x["estado"] != "unico_ok" or x["efimero_build"] is not None:
                continue
            b = next((b for b in self.builds.get(proyecto("efimero", ev["repo"]), []) if b["resolved"] == x["cd_sha"]), None)
            if b is not None:
                x["efimero_build"] = b["id"]
                self.listo(f"entrega-{ev['i']}-efimero")
            elif self.p.ahora() - x["entregado"] > ENTREGA_EFIMERO_SEG:
                raise Abortar(f"el push de {x['cd_sha'][:7]} a {cd_de(ev['repo'])} {self.V(ev['n'])['rama']} no disparó el "
                              f"despliegue del efímero en {ENTREGA_EFIMERO_SEG} s")

    # --- 4. cola del fijo -----------------------------------------------------------------------

    def cola_fijo(self):
        self.seguir_entregas_efimero()
        cola = self.s["cola_fijo"]
        en_curso = cola["en_curso"]
        if en_curso is not None:
            b = next((x for x in self.builds.get(proyecto("fijo", en_curso["repo"]), []) if x["resolved"] == en_curso["commit_dev"]), None)
            if b is None:
                return
            if en_curso.get("build_id") is None:
                en_curso["build_id"] = b["id"]
                self.log(f"fijo: build {b['id']} de la integración de {en_curso['rama']} {en_curso['repo']} k={en_curso['k']}")
            if not b["complete"]:
                return
            en_curso.update(build_status=b["status"], build_end=b["end"])
            self.log(f"fijo: build {b['id'].split(':')[1][:8]} terminó {b['status']} ({int(b['end'] - b['start'])} s)")
            self.listo("fijo-build")
            cola["en_curso"] = None
        if any(not b["complete"] for repo in ("backend", "frontend") for b in self.builds.get(proyecto("fijo", repo), [])):
            return
        s = elegir_solicitud(self.s["solicitudes_fijo"])
        if s is None:
            return
        r = self.p.integrar(s["cd"], s["cd_sha"], "dev", f"integracion {s['rama']} {s['repo']} k={s['k']} ({s['cd_sha'][:7]})")
        s.update(estado="integrada", hora=iso(self.p.ahora()))
        integ = {"version": f"c{self.c}-{s['n']}", "rama": s["rama"], "repo": s["repo"], "cd": s["cd"], "ev": s["ev"], "k": s["k"],
                 "rama_cd": s["rama_cd"], "pr": s["pr_dev"], "listo": s["listo"], "head": s["cd_sha"], "digest": s["digest"],
                 "commit_dev": r["commit"], "conflicto": r["conflicto"], "hora": iso(self.p.ahora()), "build_id": None}
        cola["integraciones"].append(integ)
        cola["en_curso"] = integ
        self.esperar("fijo-build", f"build del fijo de {s['rama']} {s['repo']} k={s['k']}")
        self.log(f"fijo: integra {s['rama_cd']} {s['repo']} k={s['k']} ({s['cd_sha'][:7]}, entregado {s['listo']}) en dev -> "
                 f"{r['commit'][:7]} (conflicto: {r['conflicto']})")

    # --- expiración por inactividad ------------------------------------------------------------

    def expira_en(self, v, modelo):
        return bool(v["expirada"]) and modelo in v["expirada"]["modelos"]

    def en_curso_de(self, v):
        """Builds de la versión que siguen en curso (compartida, despliegue efímero, integración en el fijo)."""
        en_curso = [f"build de {acciones.rama_app(v['rama'], m)} {ev['repo']}" for ev in self.eventos_de(v["n"]) for m in MODELOS
                    if ev["m"][m]["estado"] == "despachado"]
        for repo in v["repos"]:
            for b in self.builds.get(proyecto("efimero", repo), []):
                if not b["complete"] and b["resolved"] in v["cd_commits"]:
                    en_curso.append(b["id"])
        integ = self.s["cola_fijo"]["en_curso"]
        if integ is not None and integ["rama"] == v["rama"]:
            en_curso.append(integ.get("build_id") or f"integración de {v['rama']} {integ['repo']} en el fijo")
        return en_curso

    def digests_actuales(self, v, m):
        """Digest del último evento con build terminado en el modelo, por repo (los cancelados no cuentan)."""
        d = {}
        for ev in self.eventos_de(v["n"]):
            if ev["m"][m]["estado"] == "unico_ok":
                d[ev["repo"]] = ev["m"][m]["digest"]
        return d

    def expiraciones(self):
        for n in versiones_que_expiran(list(self.s["versiones"].values()), self.s["eventos"], self.p.ahora(), self.expiracion_seg):
            v = self.V(n)
            en_curso = self.en_curso_de(v)
            if en_curso:
                if f"expira-{n}" not in self.s["esperas"]:
                    self.log(f"{v['rama']} venció (sin eventos hace más de {self.expiracion_seg} s); la expiración espera a: {', '.join(en_curso)}")
                self.esperar(f"expira-{n}", f"builds en curso de {v['rama']} antes de expirar")
                continue
            self.listo(f"expira-{n}")
            ahora = self.p.ahora()
            modelos = [m for m in MODELOS if v["cierre"][m]["estado"] == "pendiente"]
            cancelados = []
            for ev in self.eventos_de(n):
                for m in MODELOS:
                    if ev["m"][m]["estado"] == "pendiente":
                        ev["m"][m]["estado"] = "cancelado"
                        cancelados.append(f"{ev['i']}-{m}")
                self.agregar(ev)
            solicitudes = [s for s in self.s["solicitudes_fijo"] if s["n"] == n and s["estado"] == "pendiente"]
            for s in solicitudes:
                s["estado"] = "cancelada"
            v["expirada"] = {"hora": iso(ahora), "ultimo_evento": iso(ultimo_evento(self.s["eventos"], n)), "modelos": modelos,
                             "eventos_cancelados": cancelados, "solicitudes_canceladas": [s["ev"] for s in solicitudes],
                             "prs_dev_cerrados": [], "finales_registradas": []}
            v["resultado_cierre"] = "declinada"
            for m in modelos:
                self.listo(f"final-{n}-{m}")
                digests = self.digests_actuales(v, m)
                f = validacion_final([r for r in v["repos"] if r in digests], digests, v["validaciones"][m]) if digests else None
                if f is not None:
                    f["tiempo_validacion_seg"] = round(f["end"] - epoch(v["apertura_m"][m]), 1)
                    f["expirada"] = True
                    v["final"][m] = f
                    v["expirada"]["finales_registradas"].append(m)
            if "fijo" in modelos:
                for pr in self.p.prs_dev():
                    if pr["head"] == rama_cd(v["rama"], "fijo"):
                        self.p.pr_cerrar(pr["cd"], pr["number"])
                        v["expirada"]["prs_dev_cerrados"].append({"cd": pr["cd"], "pr": pr["number"]})
            self.log(f"EXPIRADA {v['rama']}: último evento {v['expirada']['ultimo_evento']}; se cierra como declinada en "
                     f"{', '.join(modelos)}; validación final registrada en: {', '.join(v['expirada']['finales_registradas']) or 'ninguno'}"
                     + (f"; PR -> dev cerrados: {v['expirada']['prs_dev_cerrados']}" if v["expirada"]["prs_dev_cerrados"] else "")
                     + (f"; eventos cancelados: {cancelados}" if cancelados else ""))

    def vencida(self, v):
        ultimo = ultimo_evento(self.s["eventos"], v["n"])
        return ultimo is not None and self.p.ahora() - ultimo > self.expiracion_seg

    # --- 5. validación final --------------------------------------------------------------------

    def finales(self):
        for v in self.s["versiones"].values():
            if v["expirada"] or (self.vencida(v) and any(v["cierre"][m]["estado"] == "pendiente" for m in MODELOS)):
                continue
            for modelo in MODELOS:
                if v["final"][modelo] is not None:
                    continue
                if any(ev["m"][modelo]["estado"] != "unico_ok" for ev in self.eventos_de(v["n"])):
                    continue
                self.esperar(f"final-{v['n']}-{modelo}", f"validación final de {v['rama']} en {modelo}")
                f = validacion_final(v["repos"], self.digests_actuales(v, modelo), v["validaciones"][modelo])
                if f is None:
                    continue
                f["tiempo_validacion_seg"] = round(f["end"] - epoch(v["apertura_m"][modelo]), 1)
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
        if c["estado"] == "hecho" or (v["final"]["efimero"] is None and not self.expira_en(v, "efimero")):
            return
        if c["estado"] == "pendiente":
            c["estado"] = "en_curso"
        integrada = (v["resultado_cierre"] or v["resultado"]) == "integrada"
        for repo in v["repos"]:
            r = c["repos"].setdefault(repo, {"fase": "pr"})
            if r["fase"] == "hecho":
                continue
            cd = cd_de(repo)
            if r["fase"] == "pr" and self.p.ref_sha(cd, v["rama"]) is None:
                r.update(fase="hecho", accion="sin_rama", resultado=None)
                self.log(f"cierre efímero {v['rama']} {repo}: la rama no existe en {cd} (el repo no llegó a desplegarse); nada que cerrar")
                continue
            if r["fase"] == "pr":
                pr = self.p.pr_crear(cd, "main-efimero", v["rama"], f"{v['rama']} cierre efimero ({v['resultado']})")
                r.update(pr=pr, t_accion=self.p.ahora())
                if integrada:
                    m = self.p.integrar(cd, f"origin/{v['rama']}", "main-efimero", f"cierre {v['rama']} integrada (PR #{pr})")
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
        con_rama = [r for r in v["repos"] if c["repos"][r].get("accion") != "sin_rama"]
        if not con_rama:
            c.update(estado="hecho", destruccion={"via": "sin_entorno", "build_id": None, "end": None, "resultado": None,
                                                  "motivo_manual": None})
            self.log(f"CERRADA {v['rama']} en efímero: no llegó a tener entorno")
            return
        resultados = [c["repos"][r] for r in con_rama]
        if c["manual_extra"] is not None:
            m = c["manual_extra"]
            b = self._build_por_id(m["repo"], m["build_id"])
            if b is None or not b["complete"]:
                return
            d = self.p.destruccion(b["id"]) or {"accion": None, "resultado": None}
            m.update(status=b["status"], end=b["end"], accion=d["accion"], resultado=d["resultado"])
            resultados = resultados + [m]
            self.log(f"cierre efímero {v['rama']}: manual {b['id'].split(':')[1][:8]} {b['status']}, resultado {d['resultado']}")
        decision = decidir_cierre(resultados, con_rama)
        if decision == "hecho":
            quien = next(r for r in resultados if r.get("accion") in ("destruir", "manual")
                         and r.get("resultado") in ("ok", "nada que destruir"))
            via = quien.get("via", "webhook")
            c.update(estado="hecho", destruccion={"via": via, "build_id": quien["build_id"], "end": quien["end"],
                                                  "resultado": quien["resultado"], "motivo_manual": quien.get("nota")})
            self.listo(f"cierre-{v['n']}-extra")
            self.log(f"CERRADA {v['rama']} en efímero: entorno destruido por {via} ({quien['build_id']})")
        elif decision == "manual" and c["manual_extra"] is None:
            bid = self.p.cierre_manual(con_rama[0], v["rama"])
            c["manual_extra"] = {"repo": con_rama[0], "build_id": bid, "via": "manual", "nota": "los dos cierres esperaron al otro -cd"}
            self.esperar(f"cierre-{v['n']}-extra", f"cierre manual de {v['rama']}")
            self.log(f"cierre efímero {v['rama']}: los dos cierres esperaron al otro -cd; cierre manual {bid}")
        elif decision == "falla" and (c["manual_extra"] is None or c["manual_extra"].get("end") is not None):
            raise Abortar(f"el cierre efímero de {v['rama']} no destruyó el entorno: {resultados}")

    def cierre_fijo(self, v):
        c = v["cierre"]["fijo"]
        if c["estado"] == "hecho" or (v["final"]["fijo"] is None and not self.expira_en(v, "fijo")):
            return
        integrada = (v["resultado_cierre"] or v["resultado"]) == "integrada"
        rama_fijo = rama_cd(v["rama"], "fijo")
        for repo in v["repos"]:
            cd = cd_de(repo)
            if self.p.ref_sha(cd, rama_fijo) is None:
                c["repos"][repo] = {"accion_pr": "sin_rama", "hora": iso(self.p.ahora())}
                self.log(f"cierre fijo {v['rama']} {repo}: la rama {rama_fijo} no existe en {cd}; nada que cerrar")
                continue
            pr = self.p.pr_crear(cd, "main-fijo", rama_fijo, f"{v['rama']} cierre fijo ({v['resultado']})")
            r = {"pr": pr, "hora": iso(self.p.ahora())}
            if integrada:
                m = self.p.integrar(cd, f"origin/{rama_fijo}", "main-fijo", f"cierre {v['rama']} integrada (PR #{pr})")
                r.update(accion_pr="merge", commit=m["commit"], conflicto=m["conflicto"])
                self.s["merged_pendientes"].append({"repo_gh": cd, "pr": pr, "base": "main-fijo", "rama": v["rama"], "desde": self.p.ahora()})
            else:
                self.p.pr_cerrar(cd, pr)
                r["accion_pr"] = "close"
            c["repos"][repo] = r
            self.log(f"cierre fijo {v['rama']} {repo}: PR #{pr} {rama_fijo} -> main-fijo, "
                     f"{'merge local y push' if integrada else 'cerrado sin merge'}")
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
            hecho = {"hora": iso(self.p.ahora()), "ramas_cd": [], "prs_dev": [], "app": []}
            for repo in v["repos"]:
                cd = cd_de(repo)
                # historial de las dos ramas del -cd antes de borrarlas (control de cierre_corrida)
                v["historial_cd"][cd] = {rama_cd(v["rama"], m): self.p.historial_cd(cd, rama_cd(v["rama"], m)) for m in MODELOS}
                for pr in self.p.prs_dev():
                    if pr["cd"] == cd and pr["head"] == rama_cd(v["rama"], "fijo"):
                        self.p.pr_cerrar(cd, pr["number"])
                        hecho["prs_dev"].append({"cd": cd, "pr": pr["number"]})
                for m in MODELOS:
                    self.p.borrar_rama(cd, rama_cd(v["rama"], m))
                    hecho["ramas_cd"].append(f"{cd} {rama_cd(v['rama'], m)}")
            for m in MODELOS:
                for repo, d in v["app_pr"][m].items():
                    self.p.pr_cerrar(app_de(repo), d["pr"])
                    self.p.borrar_rama(app_de(repo), acciones.rama_app(v["rama"], m))
                    hecho["app"].append({"repo": app_de(repo), "modelo": m, "pr": d["pr"]})
            v["limpieza"] = hecho
            self.log(f"limpieza {v['rama']}: ramas del -cd borradas ({', '.join(hecho['ramas_cd'])}); PR y ramas de la app "
                     f"de los dos modelos cerrados y borrados")

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
                    "apertura": v["apertura"], "apertura_m": v["apertura_m"], "app_pr": v["app_pr"], "digest_m": v["digest_m"],
                    "modelos": {}}
            for modelo in MODELOS:
                f = v["final"][modelo]
                m = {"validacion_final": None if f is None else {
                        "build_id": f["build_id"], "repo": f["repo"], "end": iso(f["end"]), "resultado": f["resultado"],
                        "por_repo": {r: dict(x, end=iso(x["end"])) for r, x in f["por_repo"].items()}},
                     "tiempo_validacion_seg": None if f is None else f["tiempo_validacion_seg"],
                     "validaciones": [dict(x, start=iso(x["start"]), end=iso(x["end"])) for x in v["validaciones"][modelo]]}
                if f is not None and f.get("expirada"):
                    m["validacion_final"]["expirada"] = True
                if modelo == "fijo":
                    m["integraciones"] = [i for i in self.s["cola_fijo"]["integraciones"] if i["rama"] == v["rama"]]
                    m["cierre"] = v["cierre"]["fijo"]
                else:
                    m["cierre"] = v["cierre"]["efimero"]
                fila["modelos"][modelo] = m
            fila["limpieza"] = v["limpieza"]
            fila["historial_cd"] = v["historial_cd"]
            fila["expirada"] = v["expirada"]
            fila["resultado_cierre"] = v["resultado_cierre"] or v["resultado"]
            versiones.append(fila)
        return {"corrida": self.s["corrida"], "semilla": self.s["semilla"], "nivel": self.s["nivel"], "esquema": ESQUEMA,
                "regla_actualizacion": REGLA_ACTUALIZACION, "max_espera_seg": MAX_ESPERA_SEG, "simulada": self.s["simulada"],
                "inicio": iso(self.s["inicio"]), "t0": iso(self.s["t0"]), "fin": iso(self.s["fin"]), "resultado": self.s["resultado"],
                "motivo": self.s["motivo"],
                "eventos": [{k: (iso(x) if k in ("despachado",) else x) for k, x in ev.items()} for ev in self.s["eventos"]],
                "versiones": versiones, "merged": self.s.get("merged", []), "integraciones_fijo": self.s["cola_fijo"]["integraciones"],
                "solicitudes_fijo": self.s["solicitudes_fijo"], "avisos": self.s["avisos"],
                "expiracion_seg": self.expiracion_seg, "depurador": self.s["depurador"], "cierre": self.s["cierre"],
                "protocolo": self.s["protocolo"], "protocolo_verificacion": self.s.get("protocolo_verificacion")}

    def escribir_corrida(self):
        if self.escribir:
            self.archivo_corrida.write_text(json.dumps(self.resumen(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def verificar_protocolo(self) -> bool:
        """Verificación en vivo del manifiesto antes del reset. False: la corrida no arranca."""
        if self.p.simulada:
            self.s["protocolo"] = "simulado"
            return True
        if not self.verificar:
            self.s["protocolo"] = "no verificado"
            self.log("protocolo NO verificado (--sin-verificar): la corrida va a quedar inválida")
            return True
        try:
            m, diferencias = protocolo_mod.verificar_vivo(escenario=self.e)
        except Exception as error:  # noqa: BLE001 - un manifiesto ilegible o una consulta fallida tampoco arranca
            m, diferencias = None, [f"no se pudo verificar: {type(error).__name__}: {error}"]
        self.s["protocolo_verificacion"] = {"hora": iso(self.p.ahora()), "manifiesto": None if m is None else m["nombre"],
                                            "huella": None if m is None else m["huella"], "diferencias": diferencias}
        if diferencias:
            self.log(f"protocolo: {len(diferencias)} diferencias con el manifiesto; la corrida NO arranca")
            for x in diferencias:
                self.log(f"  NO COINCIDE: {x}")
            return False
        self.s["protocolo"] = m["huella"]
        self.log(f"protocolo {m['nombre']} verificado: huella {m['huella']}")
        return True

    def correr(self):
        self.s["inicio"] = self.p.ahora()
        if not self.verificar_protocolo():
            self.s.update(fin=self.p.ahora(), resultado="no_arrancada", motivo="el protocolo no coincide con el manifiesto")
            self.guardar()
            self.escribir_corrida()
            return 5
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
                self.expiraciones()
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
            self.finalizar()

    def finalizar(self):
        """Depurador (limpia si la corrida terminó; solo busca si se abortó), registro y cierre de corrida."""
        if not self.p.simulada:
            abortada = self.s["resultado"] != "ok"
            try:
                self.log("depurador" + (" (solo búsqueda: la corrida se abortó)" if abortada else ""))
                self.s["depurador"] = depurador_mod.depurar(solo_buscar=abortada, log=lambda m: self.log("  " + m))
            except Exception as error:  # noqa: BLE001
                self.s["depurador"] = {"error": f"{type(error).__name__}: {error}", "encontrados": [{"tipo": "error_depurador"}]}
                self.log(f"ERROR del depurador: {error}")
        self.guardar()
        self.escribir_corrida()
        if not self.p.simulada and self.escribir:
            try:
                informe = cierre_corrida.cerrar(self.c, depurador=self.s["depurador"], log=self.log)
                self.s["cierre"] = {"valida": informe["valida"], "motivos": informe["motivos"]}
            except Exception as error:  # noqa: BLE001
                self.s["cierre"] = {"valida": False, "motivos": [f"cierre_corrida falló: {type(error).__name__}: {error}"]}
                self.log(f"ERROR en cierre_corrida: {error}")
            self.guardar()
            self.escribir_corrida()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ejecuta una corrida completa en los dos modelos")
    parser.add_argument("--escenario", required=True)
    parser.add_argument("--sin-reset", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sin-verificar", action="store_true",
                        help="SOLO PARA PRUEBAS: no verifica el manifiesto; protocolo = 'no verificado' y la corrida queda inválida")
    parser.add_argument("--expiracion-seg", type=int, default=EXPIRACION_SEG,
                        help="una versión sin eventos propios durante más de este tiempo expira (por defecto 4 días)")
    args = parser.parse_args(argv)
    escenario = json.loads(Path(args.escenario).read_text(encoding="utf-8"))
    if args.dry_run:
        ej = Ejecutor(escenario, PlataformaSimulada(escenario), sin_reset=args.sin_reset, escribir=False,
                      expiracion_seg=args.expiracion_seg)
        codigo = ej.correr()
        print(json.dumps({v["rama"]: {m: v["modelos"][m]["tiempo_validacion_seg"] for m in MODELOS} | {
            "cierre_efimero": (v["modelos"]["efimero"]["cierre"].get("destruccion") or {}).get("via")}
            for v in ej.resumen()["versiones"]}, ensure_ascii=False))
        return codigo
    return Ejecutor(escenario, PlataformaReal(escenario), sin_reset=args.sin_reset, expiracion_seg=args.expiracion_seg,
                    verificar=not args.sin_verificar).correr()


if __name__ == "__main__":
    sys.exit(main())
