#!/usr/bin/env python3
"""Cierre de corrida: validez y tablas de datos.

    python cierre_corrida.py --corrida N

Lee registros/corrida-c<N>.json (del ejecutor) y escenarios/c<N>.json, baja los validacion.json de cada
validación (y, si un aprovisionamiento falló, el log del build) y:
  - decide la validez de la corrida y el motivo de cada criterio que falle;
  - regenera las filas de la corrida en datos/versiones.csv y datos/corridas.csv (--destino datos/pruebas para
    las corridas de prueba);
  - escribe registros/cierre-c<N>.json.

Criterios de validez: todos los eventos del escenario despachados (los cancelados por expiración cuentan);
todos los builds únicos SUCCEEDED; cada versión con validación final en los dos modelos o con expiración
registrada; el depurador sin restos; la corrida no abortada; ningún aprovisionamiento fallido por cuota o
límite del proveedor ("limit", "quota", "Throttl", "exceeded" en el validacion.json o en el log del build).

Esquema 2 (dos ramas por modelo, protocolo v4): además, los controles de controles_dos_ramas (entrega al modelo
equivocado, builds duplicados, colisión de tag, un modelo que recibe y el otro no, commit k fuera de orden), cada uno
con su regla (motivo de invalidez o aviso); t_apertura es el createdAt del primer PR de la rama del modelo y
espera_fijo_s arranca en la entrega al fijo (listo = fin del build de la rama del fijo).

Estadísticas por modelo (corridas.csv): sobre las versiones con validación final que no expiraron. Mediana
estándar y p90 por interpolación lineal (statistics.quantiles, method="inclusive"); con una sola versión,
los dos valen ese valor. versiones_por_hora = versiones / (último fin de validación final - t0) * 3600.
espera_acumulada_s: solo el fijo (suma de espera_fijo_s).
"""

import argparse
import csv
import json
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import reset as reset_mod

RAIZ = Path(__file__).resolve().parent
REGISTROS = RAIZ / "registros"
ESCENARIOS = RAIZ / "escenarios"
DATOS = RAIZ / "datos"
REGION = "us-east-2"
MODELOS = ("efimero", "fijo")
TOLERANCIA_S = 1.0          # comparación de horas (redondeo a segundos de los ISO del registro)
RETRASO_AVISO_S = 120       # despacho más de esto después de T: aviso
DESFASE_APERTURA_S = 60     # createdAt de los dos modelos más separados que esto: aviso
BUCKETS = {"efimero": ("tf-efimero", "tesis-efimero-registros-398846004421"),
           "fijo": ("tf-fijo", "tesis-fijo-registros-491265021424")}
PATRON_PROVEEDOR = re.compile(r"limit|quota|Throttl|exceeded", re.IGNORECASE)

COLUMNAS_VERSIONES = [
    "corrida", "nivel", "semilla", "n", "rama", "tipo", "resultado_escenario", "modelo", "t_apertura",
    "t_fin_validacion_final", "tiempo_validacion_s", "resultado_validacion_final", "validaciones", "espera_fijo_s",
    "aprovisionamiento_s", "flaky", "unexpected", "expirada", "build_final",
]
COLUMNAS_CORRIDAS = [
    "corrida", "nivel", "semilla", "modelo", "valida", "motivo_invalidez", "versiones", "mediana_tiempo_validacion_s",
    "p90_tiempo_validacion_s", "versiones_por_hora", "espera_acumulada_s", "flaky_total", "inicio", "fin", "protocolo",
]


def epoch(texto):
    if texto in (None, ""):
        return None
    if isinstance(texto, (int, float)):
        return float(texto)
    return datetime.fromisoformat(texto.replace("Z", "+00:00")).timestamp()


def iso(e):
    return None if e is None else datetime.fromtimestamp(e, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------------------------------
# Funciones puras


def percentiles(valores: list):
    """(mediana, p90); None si no hay valores."""
    if not valores:
        return None, None
    if len(valores) == 1:
        return valores[0], valores[0]
    return statistics.median(valores), statistics.quantiles(valores, n=10, method="inclusive")[8]


def falla_proveedor(doc: dict, log: str = "") -> bool:
    """Aprovisionamiento fallido con un mensaje de cuota o límite del proveedor."""
    aprov = (doc or {}).get("aprovisionamiento") or {}
    if aprov.get("resultado") != "fallo":
        return False
    return bool(PATRON_PROVEEDOR.search(json.dumps(doc, ensure_ascii=False)) or PATRON_PROVEEDOR.search(log or ""))


def evaluar_validez(corrida: dict, escenario: dict, docs: dict, logs: dict, depurador) -> list:
    """Motivos de invalidez (lista vacía si la corrida es válida). docs = {build_id: validacion.json};
    logs = {build_id: texto del log} (solo de los aprovisionamientos fallidos); depurador = informe o None."""
    motivos = []
    protocolo = corrida.get("protocolo")
    if protocolo in (None, "no verificado", "simulado"):
        motivos.append(f"protocolo no verificado ({protocolo or 'corrida previa al protocolo'})")
    if corrida.get("resultado") != "ok":
        motivos.append(f"corrida abortada: {corrida.get('motivo')}")
    eventos = corrida.get("eventos") or []
    if len(eventos) != len(escenario["eventos_ordenados"]):
        motivos.append(f"el registro tiene {len(eventos)} eventos y el escenario {len(escenario['eventos_ordenados'])}")
    sin_despachar = [e for e in eventos if e.get("estado") in (None, "pendiente")]
    if sin_despachar:
        motivos.append("eventos sin despachar: " + ", ".join(f"c{escenario['corrida']}-{e['n']} {e['repo']} t={e['t']}" for e in sin_despachar))
    malos = [] if corrida.get("esquema", 1) == 2 else [e for e in eventos if e.get("estado") not in ("pendiente", "cancelado")
                                                        and (e.get("unico") or {}).get("status") != "SUCCEEDED"]
    if malos:
        motivos.append("builds únicos no SUCCEEDED: " + ", ".join(
            f"c{escenario['corrida']}-{e['n']} {e['repo']} {(e.get('unico') or {}).get('build_id')} {(e.get('unico') or {}).get('status')}" for e in malos))
    for v in corrida.get("versiones", []):
        if v.get("expirada"):
            continue
        for m in MODELOS:
            if not v["modelos"][m].get("validacion_final"):
                motivos.append(f"{v['rama']} sin validación final en {m} ni expiración")
    if depurador is None:
        motivos.append("sin informe del depurador")
    elif depurador.get("encontrados"):
        motivos.append(f"el depurador encontró {len(depurador['encontrados'])} restos: "
                       + ", ".join(sorted({r['tipo'] for r in depurador['encontrados']})))
    for bid, doc in docs.items():
        if falla_proveedor(doc, logs.get(bid, "")):
            motivos.append(f"inválida por proveedor: aprovisionamiento fallido por cuota o límite en {bid}")
    if corrida.get("esquema", 1) == 2:
        motivos += controles_dos_ramas(corrida)["motivos"]
    return motivos


def _rama_cd(rama, modelo):
    return rama if modelo == "efimero" else rama.replace("release/", "release/fijo/", 1)


def controles_dos_ramas(corrida: dict) -> dict:
    """Controles del diseño de dos ramas (esquema 2) sobre registros/corrida-cN.json. {motivos, avisos}: cada motivo
    invalida la corrida; los avisos no. Las versiones expiradas solo se revisan en lo que llegó a pasar."""
    motivos, avisos = [], []
    c = corrida["corrida"]
    versiones = {v["n"]: v for v in corrida.get("versiones", [])}
    eventos = corrida.get("eventos") or []
    digests = {(n, m): {ev["m"][m].get("digest") for ev in eventos if ev["n"] == n and ev["m"][m].get("digest")}
               for n in versiones for m in MODELOS}
    commits = {(n, m, r): {ev["m"][m].get("commit") for ev in eventos if ev["n"] == n and ev["repo"] == r and ev["m"][m].get("commit")}
               for n in versiones for m in MODELOS for r in ("backend", "frontend")}

    def nombre(ev, m=None):
        return f"{c}-{ev['n']} {ev['repo']} k={ev.get('actualizacion')}" + (f" en {m}" if m else "")

    tags = {}
    for ev in eventos:
        v = versiones.get(ev["n"]) or {}
        expirada = bool(v.get("expirada"))
        xs = ev["m"]
        # (4) un modelo recibe y el otro no
        for m in MODELOS:
            x = xs[m]
            if x.get("estado") == "cancelado":
                if not expirada:
                    motivos.append(f"un modelo recibe y el otro no: {nombre(ev, m)} cancelado sin expiración")
                continue
            if x.get("estado") != "unico_ok" or not x.get("digest") or not x.get("cd_sha") or x.get("entregado") is None:
                motivos.append(f"un modelo recibe y el otro no: {nombre(ev, m)} sin build y entrega completos ({x.get('estado')})")
                continue
            if not expirada and not any(val.get("digest") == x["digest"] and val.get("end")
                                        for val in v["modelos"][m].get("validaciones", [])):
                motivos.append(f"un modelo recibe y el otro no: {nombre(ev, m)} sin validación de su imagen {x['digest'][:19]}")
            # (2) builds duplicados
            u = x.get("unico") or {}
            if u.get("status") != "SUCCEEDED":
                motivos.append(f"build de {nombre(ev, m)} {u.get('build_id')} {u.get('status')}")
            ok = [b for b in x.get("builds") or [] if b.get("status") == "SUCCEEDED"]
            if len(ok) > 1 or x.get("duplicados"):
                avisos.append(f"builds duplicados del webhook en {nombre(ev, m)}: "
                              f"{', '.join(b['build_id'] for b in x.get('duplicados') or [])} (escribió el -cd {u.get('build_id')})")
            if x.get("builds") and u.get("build_id") not in [b["id"] for b in x["builds"]]:
                motivos.append(f"builds duplicados: el escritor {u.get('build_id')} de {nombre(ev, m)} no está entre sus builds")
            # (1) entrega al modelo equivocado: lo que escribió el build
            t = x.get("tag") or {}
            if t.get("modelo") != m or t.get("commit") != x.get("commit") or t.get("rama") != v.get("rama"):
                motivos.append(f"entrega al modelo equivocado: tag.json de {nombre(ev, m)} con modelo {t.get('modelo')!r}, "
                               f"commit {str(t.get('commit'))[:7]}, rama {t.get('rama')}")
            # (3) colisión de tag
            if t.get("tag") != (x.get("commit") or "")[:7]:
                motivos.append(f"colisión de tag: {nombre(ev, m)} tiene tag {t.get('tag')} y commit {str(x.get('commit'))[:7]}")
            if t.get("tag") in tags and tags[t.get("tag")] != (ev["i"], m):
                motivos.append(f"colisión de tag: {t.get('tag')} en {nombre(ev, m)} y en otro evento")
            tags[t.get("tag")] = (ev["i"], m)
            img = x.get("imagen")
            if not img or img.get("pushed_at") is None:
                avisos.append(f"sin datos de ECR de la imagen de {nombre(ev, m)}; no se pudo comprobar que la construyó su build")
            else:
                if t.get("tag") not in (img.get("tags") or []):
                    motivos.append(f"colisión de tag: la imagen {x['digest'][:19]} de {nombre(ev, m)} no lleva el tag {t.get('tag')}")
                if u.get("start") is not None and u.get("end") is not None and not (
                        u["start"] - TOLERANCIA_S <= img["pushed_at"] <= u["end"] + TOLERANCIA_S):
                    motivos.append(f"colisión de tag: la imagen de {nombre(ev, m)} se subió fuera de su build "
                                   f"({iso(img['pushed_at'])}, build {iso(u['start'])}–{iso(u['end'])})")
        e, f = xs["efimero"], xs["fijo"]
        if e.get("estado") == "unico_ok" and f.get("estado") == "unico_ok":
            if e.get("digest") == f.get("digest") or e.get("commit") == f.get("commit"):
                motivos.append(f"colisión de tag: {nombre(ev)} con la misma imagen o el mismo commit en los dos modelos")
            if e.get("arbol") != f.get("arbol"):
                motivos.append(f"{nombre(ev)}: los commits de los dos modelos no tienen el mismo árbol")
        # (5) commit k fuera de orden: la regla de actualización y el orden por repo y modelo
        for m in MODELOS:
            x = xs[m]
            if x.get("estado") != "unico_ok" or ev.get("accion") != "actualizacion":
                continue
            desp = epoch(ev_desp(x))
            if x.get("T") is None or desp is None:
                motivos.append(f"commit k fuera de orden: {nombre(ev, m)} sin T registrado")
                continue
            if desp < x["T"] - TOLERANCIA_S:
                motivos.append(f"commit k fuera de orden: {nombre(ev, m)} despachado {desp - x['T']:.0f} s antes de T")
            elif desp - x["T"] > RETRASO_AVISO_S:
                avisos.append(f"{nombre(ev, m)} despachado {desp - x['T']:.0f} s después de T")
            if abs(x["T"] - (x.get("F") or 0) - (ev.get("brecha") or 0)) > TOLERANCIA_S:
                motivos.append(f"commit k fuera de orden: {nombre(ev, m)} con T distinto de F + brecha")
            for val in v["modelos"][m].get("validaciones", []):
                ini, fin = epoch(val.get("start")), epoch(val.get("end"))
                if ini is not None and fin is not None and ini < desp - TOLERANCIA_S and fin > desp + TOLERANCIA_S:
                    motivos.append(f"commit k fuera de orden: {nombre(ev, m)} despachado con la validación {val['build_id']} en curso")
    # orden por versión, repo y modelo
    for n in versiones:
        for r in ("backend", "frontend"):
            for m in MODELOS:
                xs = [ev for ev in eventos if ev["n"] == n and ev["repo"] == r and ev["m"][m].get("estado") == "unico_ok"]
                for a, b in zip(xs, xs[1:]):
                    if epoch(ev_desp(b["m"][m])) < (a["m"][m].get("unico") or {}).get("end", 0) - TOLERANCIA_S:
                        motivos.append(f"commit k fuera de orden: {nombre(b, m)} despachado antes del fin del build de k={a.get('actualizacion')}")
    # integraciones del fijo: cada solicitud una vez, en orden de k por repo y por orden de entrega
    integs = corrida.get("integraciones_fijo") or []
    vistos = {}
    for i in integs:
        n = int(str(i["version"]).rsplit("-", 1)[1])
        if i.get("digest") not in digests.get((n, "fijo"), set()):
            motivos.append(f"entrega al modelo equivocado: integración en dev de {i['rama']} {i['repo']} con un digest que no es del fijo")
        clave = (i["rama"], i["repo"])
        if i.get("k") is not None and vistos.get(clave, -1) >= i["k"]:
            motivos.append(f"commit k fuera de orden: el fijo integró {i['rama']} {i['repo']} k={i['k']} después de k={vistos[clave]}")
        vistos[clave] = max(vistos.get(clave, -1), i.get("k") if i.get("k") is not None else -1)
    sols = corrida.get("solicitudes_fijo") or []
    for s in sols:
        hechas = [i for i in integs if i.get("ev") == s["ev"]]
        if s.get("estado") == "integrada" and len(hechas) != 1:
            motivos.append(f"el fijo integró {len(hechas)} veces la solicitud de {s['rama']} {s['repo']} k={s['k']}")
    for i in integs:
        antes = [s for s in sols if epoch(s["listo"]) < epoch(i["listo"]) - TOLERANCIA_S and epoch(s["listo"]) <= epoch(i["hora"])
                 and s.get("estado") == "integrada" and epoch(s.get("hora")) > epoch(i["hora"])]
        if antes:
            motivos.append(f"commit k fuera de orden: el fijo integró {i['rama']} {i['repo']} k={i['k']} antes que solicitudes "
                           f"entregadas antes ({', '.join(s['rama'] + ' ' + s['repo'] + ' k=' + str(s['k']) for s in antes)})")
    # validaciones e historial del -cd: cada modelo desplegó solo imágenes de su rama
    for n, v in versiones.items():
        for m in MODELOS:
            otro = "fijo" if m == "efimero" else "efimero"
            for val in v["modelos"][m].get("validaciones", []):
                if val.get("digest") in digests[(n, otro)]:
                    motivos.append(f"entrega al modelo equivocado: la validación {val['build_id']} del {m} desplegó una imagen del {otro}")
                elif val.get("digest") not in digests[(n, m)]:
                    motivos.append(f"entrega al modelo equivocado: la validación {val['build_id']} del {m} desplegó una imagen "
                                   f"que no es de ningún evento de {v['rama']}")
        for cd, ramas in (v.get("historial_cd") or {}).items():
            repo = cd.split("-")[1]
            for m in MODELOS:
                for h in ramas.get(_rama_cd(v["rama"], m), []):
                    if h.get("modelo") != m or h.get("commit") not in commits[(n, m, repo)]:
                        motivos.append(f"entrega al modelo equivocado: {cd} {_rama_cd(v['rama'], m)} tiene el commit {h['sha'][:7]} "
                                       f"con modelo {h.get('modelo')!r} y commit de la app {str(h.get('commit'))[:7]}")
        if not v.get("expirada") and v.get("limpieza") is not None and not v.get("historial_cd"):
            avisos.append(f"{v['rama']}: sin historial de las ramas del -cd")
        ap = v.get("apertura_m") or {}
        if ap.get("efimero") and ap.get("fijo") and abs(epoch(ap["efimero"]) - epoch(ap["fijo"])) > DESFASE_APERTURA_S:
            avisos.append(f"{v['rama']}: aperturas de los dos modelos separadas {abs(epoch(ap['efimero']) - epoch(ap['fijo'])):.0f} s")
    return {"motivos": list(dict.fromkeys(motivos)), "avisos": list(dict.fromkeys(avisos))}


def ev_desp(x):
    """Hora de despacho del modelo (epoch del registro)."""
    return x.get("despachado")


def inicio_solicitud(integracion: dict, fechas_commit: dict):
    """Una solicitud de integración empieza cuando existe su punta: el más tardío entre el createdAt del PR
    hacia dev y la fecha del commit de la punta integrada (una actualización empuja una punta nueva al mismo PR).
    Esquema 2: cuando se entregó al fijo (listo = fin del build de la rama del fijo)."""
    if integracion.get("listo") is not None:
        return epoch(integracion["listo"])
    creado = epoch(integracion["pr_created_at"])
    punta = fechas_commit.get(integracion.get("head"))
    return max(creado, punta) if punta is not None else creado


def filas_versiones(corrida: dict, escenario: dict, docs: dict, fechas_commit: dict = None) -> list:
    fechas_commit = fechas_commit or {}
    filas = []
    for v in corrida["versiones"]:
        for m in MODELOS:
            datos = v["modelos"][m]
            f = datos.get("validacion_final")
            vals = datos.get("validaciones", [])
            flaky = unexpected = 0
            aprov = None
            for x in vals:
                d = docs.get(x["build_id"]) or {}
                stats = (d.get("suite") or {}).get("stats") or {}
                flaky += stats.get("flaky") or 0
                unexpected += stats.get("unexpected") or 0
                ap = d.get("aprovisionamiento") or {}
                if m == "efimero" and ap.get("creado") and (ap.get("crear_entorno") or {}).get("total_seg") is not None:
                    aprov = (aprov or 0) + ap["crear_entorno"]["total_seg"]
            espera = None
            if m == "fijo":
                espera = round(sum(epoch(i["hora"]) - inicio_solicitud(i, fechas_commit) for i in datos.get("integraciones", [])), 1)
            filas.append({
                "corrida": corrida["corrida"], "nivel": corrida["nivel"], "semilla": corrida["semilla"], "n": v["n"],
                "rama": v["rama"], "tipo": v["tipo"], "resultado_escenario": v["resultado_escenario"], "modelo": m,
                "t_apertura": (v["apertura_m"][m] if corrida.get("esquema", 1) == 2 else v["apertura"]),
                "t_fin_validacion_final": f["end"] if f else None,
                "tiempo_validacion_s": datos.get("tiempo_validacion_seg") if f else None,
                "resultado_validacion_final": f["resultado"] if f else None, "validaciones": len(vals),
                "espera_fijo_s": espera, "aprovisionamiento_s": aprov if m == "efimero" else None,
                "flaky": flaky, "unexpected": unexpected, "expirada": bool(v.get("expirada")),
                "build_final": f["build_id"] if f else None,
            })
    return filas


def filas_corridas(corrida: dict, filas_v: list, motivos: list) -> list:
    t0 = epoch(corrida["t0"])
    filas = []
    for m in MODELOS:
        mias = [f for f in filas_v if f["modelo"] == m]
        validas = [f for f in mias if f["t_fin_validacion_final"] and not f["expirada"]]
        tiempos = [f["tiempo_validacion_s"] for f in validas]
        med, p90 = percentiles(tiempos)
        ultimo = max((epoch(f["t_fin_validacion_final"]) for f in validas), default=None)
        por_hora = round(len(validas) / (ultimo - t0) * 3600, 3) if validas and ultimo and ultimo > t0 else None
        filas.append({
            "corrida": corrida["corrida"], "nivel": corrida["nivel"], "semilla": corrida["semilla"], "modelo": m,
            "valida": not motivos, "motivo_invalidez": "; ".join(motivos) or None, "versiones": len(validas),
            "mediana_tiempo_validacion_s": None if med is None else round(med, 1),
            "p90_tiempo_validacion_s": None if p90 is None else round(p90, 1), "versiones_por_hora": por_hora,
            "espera_acumulada_s": round(sum(f["espera_fijo_s"] or 0 for f in mias), 1) if m == "fijo" else None,
            "flaky_total": sum(f["flaky"] for f in mias), "inicio": corrida["t0"], "fin": corrida["fin"],
            "protocolo": corrida.get("protocolo") or "previo al protocolo",
        })
    return filas


def _clave(fila):
    return (int(str(fila["corrida"]).lstrip("c")), int(fila.get("n") or 0), fila["modelo"])


def actualizar_csv(ruta: Path, columnas: list, filas: list, corrida: str):
    """Reemplaza las filas de la corrida (si ya estaban) y deja el archivo ordenado."""
    existentes = []
    if ruta.exists():
        with ruta.open(newline="", encoding="utf-8") as f:
            existentes = [r for r in csv.DictReader(f) if r["corrida"] != corrida]

    def texto(x):
        if x is None:
            return ""
        if isinstance(x, bool):
            return "true" if x else "false"
        return str(x)
    nuevas = [{c: texto(f.get(c)) for c in columnas} for f in filas]
    todas = sorted(existentes + nuevas, key=_clave)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with ruta.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columnas, lineterminator="\n")
        w.writeheader()
        w.writerows(todas)


# --------------------------------------------------------------------------------------------------
# Lecturas reales


def _correr(args):
    return subprocess.run(args, text=True, capture_output=True, env=reset_mod._entorno())


def bajar_validacion(modelo, corrida, build_id):
    perfil, bucket = BUCKETS[modelo]
    r = _correr(["aws", "s3", "cp", f"s3://{bucket}/validaciones/corrida={corrida}/{build_id.split(':', 1)[1]}/validacion.json",
                 "-", "--only-show-errors", "--profile", perfil, "--region", REGION])
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def log_build(perfil, build_id):
    r = _correr(["aws", "codebuild", "batch-get-builds", "--ids", build_id, "--profile", perfil, "--region", REGION,
                 "--query", "builds[0].logs.[groupName,streamName]", "--output", "json"])
    if r.returncode != 0:
        return ""
    g, s = json.loads(r.stdout)
    lineas, token = [], None
    while True:
        args = ["aws", "logs", "get-log-events", "--log-group-name", g, "--log-stream-name", s, "--start-from-head",
                "--profile", perfil, "--region", REGION, "--output", "json"]
        if token:
            args += ["--next-token", token]
        r = _correr(args)
        if r.returncode != 0:
            break
        d = json.loads(r.stdout)
        lineas += [e["message"] for e in d["events"]]
        if d["nextForwardToken"] == token:
            break
        token = d["nextForwardToken"]
    return "".join(lineas)


def fecha_commit(repo, sha):
    r = _correr(["gh", "api", f"repos/tesis-entornos/{repo}/commits/{sha}", "-q", ".commit.committer.date"])
    return epoch(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def cerrar(n: int, depurador=None, log=print, destino=DATOS):
    corrida = json.loads((REGISTROS / f"corrida-c{n}.json").read_text(encoding="utf-8"))
    escenario = json.loads((ESCENARIOS / f"c{n}.json").read_text(encoding="utf-8"))
    nota_depurador = None
    if depurador is None:
        depurador = corrida.get("depurador")
    if depurador is None:
        import depurador as dep
        depurador = dep.depurar(solo_buscar=True, log=lambda *_: None)
        nota_depurador = "el registro de la corrida no tiene informe del depurador; se usó una búsqueda actual (solo lectura)"
        log(f"cierre c{n}: {nota_depurador}")
    docs, logs = {}, {}
    for v in corrida["versiones"]:
        for m in MODELOS:
            for x in v["modelos"][m].get("validaciones", []):
                d = bajar_validacion(m, corrida["corrida"], x["build_id"])
                if d is None:
                    log(f"AVISO no se pudo leer el validacion.json de {x['build_id']}")
                    continue
                docs[x["build_id"]] = d
                if m == "efimero" and ((d.get("aprovisionamiento") or {}).get("resultado") == "fallo"):
                    logs[x["build_id"]] = log_build(BUCKETS[m][0], x["build_id"])
    motivos = evaluar_validez(corrida, escenario, docs, logs, depurador)
    avisos = controles_dos_ramas(corrida)["avisos"] if corrida.get("esquema", 1) == 2 else []
    fechas = {}
    for i in corrida.get("integraciones_fijo", []):
        if i.get("listo") is None and i.get("head") and i["head"] not in fechas:
            fechas[i["head"]] = fecha_commit(i["cd"], i["head"])
    filas_v = filas_versiones(corrida, escenario, docs, fechas)
    filas_c = filas_corridas(corrida, filas_v, motivos)
    actualizar_csv(Path(destino) / "versiones.csv", COLUMNAS_VERSIONES, filas_v, corrida["corrida"])
    actualizar_csv(Path(destino) / "corridas.csv", COLUMNAS_CORRIDAS, filas_c, corrida["corrida"])
    informe = {"corrida": corrida["corrida"], "valida": not motivos, "motivos": motivos, "avisos": avisos, "nota_depurador": nota_depurador,
               "depurador": depurador, "versiones": filas_v, "corridas": filas_c, "hora": iso(datetime.now(timezone.utc).timestamp())}
    (REGISTROS / f"cierre-c{n}.json").write_text(json.dumps(informe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"cierre c{n}: {'VÁLIDA' if not motivos else 'INVÁLIDA'}" + ("" if not motivos else " - " + "; ".join(motivos)))
    return informe


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validez y tablas de datos de una corrida")
    parser.add_argument("--corrida", type=int, required=True)
    parser.add_argument("--destino", default=str(DATOS),
                        help="carpeta de las tablas (por defecto datos/; las corridas de prueba van en datos/pruebas/)")
    args = parser.parse_args(argv)
    informe = cerrar(args.corrida, destino=args.destino)
    return 0 if informe["valida"] else 1


if __name__ == "__main__":
    sys.exit(main())
