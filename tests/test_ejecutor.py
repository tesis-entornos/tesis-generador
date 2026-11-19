import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import ejecutor  # noqa: E402
import escenario  # noqa: E402


def escenario_c96():
    """c96 (nivel 3, semilla 9603) tal como se generó con el sorteo de v1 (commit 9c93b47): los tests de abajo
    dependen de su línea de tiempo, que el sorteo de aperturas de v2 cambia."""
    return json.loads((RAIZ / "escenarios" / "c96.json").read_text(encoding="utf-8"))


def sol(ev, listo, estado="pendiente"):
    return {"ev": ev, "listo": ejecutor.iso(listo), "estado": estado}


class Cola(unittest.TestCase):
    def test_por_orden_de_entrega(self):
        s = [sol(4, 300), sol(2, 150), sol(7, 200)]
        self.assertEqual(ejecutor.elegir_solicitud(s)["ev"], 2)

    def test_empate_el_evento_anterior(self):
        self.assertEqual(ejecutor.elegir_solicitud([sol(5, 100), sol(3, 100)])["ev"], 3)

    def test_salta_integradas_y_canceladas(self):
        s = [sol(1, 100, "integrada"), sol(2, 110, "cancelada"), sol(3, 120)]
        self.assertEqual(ejecutor.elegir_solicitud(s)["ev"], 3)

    def test_vacia(self):
        self.assertIsNone(ejecutor.elegir_solicitud([]))
        self.assertIsNone(ejecutor.elegir_solicitud([sol(1, 100, "integrada")]))


def ev(i, n, t, repo, accion):
    return {"i": i, "n": n, "t": t, "repo": repo, "accion": accion}


class Brechas(unittest.TestCase):
    def test_cadena_desde_la_apertura_y_entre_actualizaciones(self):
        evs = [ev(0, 1, 0, "backend", "apertura"), ev(1, 1, 300, "backend", "actualizacion"),
               ev(2, 1, 300, "backend", "actualizacion"), ev(3, 1, 750, "backend", "actualizacion")]
        self.assertEqual(ejecutor.brechas(evs), {1: (0, 300), 2: (1, 0), 3: (2, 450)})

    def test_combinacion_la_segunda_apertura_no_corta_la_cadena(self):
        evs = [ev(0, 2, 100, "frontend", "apertura"), ev(1, 2, 400, "backend", "apertura"),
               ev(2, 2, 450, "backend", "actualizacion"), ev(3, 2, 900, "frontend", "actualizacion")]
        self.assertEqual(ejecutor.brechas(evs), {2: (0, 350), 3: (2, 450)})

    def test_versiones_independientes(self):
        evs = [ev(0, 1, 0, "backend", "apertura"), ev(1, 2, 10, "frontend", "apertura"),
               ev(2, 2, 20, "frontend", "actualizacion"), ev(3, 1, 30, "backend", "actualizacion")]
        self.assertEqual(ejecutor.brechas(evs), {2: (1, 10), 3: (0, 30)})

    def test_las_del_plan_salen_de_los_escenarios_sin_re_sortear(self):
        for c in (60, 61, 70, 81):
            e = json.loads((RAIZ / "escenarios" / f"c{c}.json").read_text())
            evs = [dict(x, i=i) for i, x in enumerate(e["eventos_ordenados"])]
            b = ejecutor.brechas(evs)
            self.assertEqual(len(b), sum(1 for x in evs if x["accion"] == "actualizacion"))
            self.assertTrue(all(0 <= g <= 600 for _, g in b.values()))


class EstadoYBuilds(unittest.TestCase):
    def test_estado_agregado(self):
        self.assertEqual(ejecutor.estado_agregado(["unico_ok", "unico_ok"]), "unico_ok")
        self.assertEqual(ejecutor.estado_agregado(["unico_ok", "despachado"]), "despachado")
        self.assertEqual(ejecutor.estado_agregado(["pendiente", "unico_ok"]), "pendiente")
        self.assertEqual(ejecutor.estado_agregado(["cancelado", "cancelado"]), "cancelado")
        self.assertEqual(ejecutor.estado_agregado(["cancelado", "unico_ok"]), "parcial")

    def test_build_unico_escritor_y_duplicados(self):
        b = lambda i, st, start, complete=True: {"id": i, "status": st, "start": start, "complete": complete}
        self.assertEqual(ejecutor.elegir_build_unico([])[0], "esperar")
        self.assertEqual(ejecutor.elegir_build_unico([b("x", "IN_PROGRESS", 1, False)])[0], "esperar")
        self.assertEqual(ejecutor.elegir_build_unico([b("x", "FAILED", 1)])[0], "falla")
        d, w, dup = ejecutor.elegir_build_unico([b("y", "SUCCEEDED", 20), b("x", "SUCCEEDED", 10), b("z", "FAILED", 11)])
        self.assertEqual((d, w["id"], sorted(x["id"] for x in dup)), ("ok", "x", ["y", "z"]))

    def test_rama_cd(self):
        self.assertEqual(ejecutor.rama_cd("release/c1-2", "efimero"), "release/c1-2")
        self.assertEqual(ejecutor.rama_cd("release/c1-2", "fijo"), "release/fijo/c1-2")


def val(repo, digest, end, build, resultado="ok"):
    return {"repo": repo, "digest": digest, "end": end, "build_id": build, "resultado": resultado}


class ValidacionFinal(unittest.TestCase):
    def test_un_repo_ignora_digests_viejos(self):
        vals = [val("backend", "d0", 100, "b1"), val("backend", "d1", 300, "b2")]
        f = ejecutor.validacion_final(["backend"], {"backend": "d1"}, vals)
        self.assertEqual((f["build_id"], f["end"]), ("b2", 300))

    def test_falta_el_digest_mas_reciente(self):
        vals = [val("backend", "d0", 100, "b1")]
        self.assertIsNone(ejecutor.validacion_final(["backend"], {"backend": "d1"}, vals))
        self.assertIsNone(ejecutor.validacion_final(["backend"], {}, vals))

    def test_sin_terminar_no_cuenta(self):
        vals = [val("backend", "d1", None, "b1")]
        self.assertIsNone(ejecutor.validacion_final(["backend"], {"backend": "d1"}, vals))

    def test_combinacion_la_ultima_de_las_dos(self):
        vals = [val("backend", "b-d1", 500, "be2"), val("frontend", "f-d0", 400, "fe1"), val("backend", "b-d0", 200, "be1")]
        digests = {"backend": "b-d1", "frontend": "f-d0"}
        f = ejecutor.validacion_final(["backend", "frontend"], digests, vals)
        self.assertEqual((f["build_id"], f["repo"], f["end"]), ("be2", "backend", 500))
        self.assertEqual(set(f["por_repo"]), {"backend", "frontend"})

    def test_combinacion_falta_un_repo(self):
        vals = [val("backend", "b-d1", 500, "be2")]
        self.assertIsNone(ejecutor.validacion_final(["backend", "frontend"], {"backend": "b-d1", "frontend": "f"}, vals))

    def test_la_primera_con_ese_digest_y_falla_es_dato(self):
        vals = [val("frontend", "f", 900, "x2"), val("frontend", "f", 700, "x1", "falla")]
        f = ejecutor.validacion_final(["frontend"], {"frontend": "f"}, vals)
        self.assertEqual((f["build_id"], f["resultado"]), ("x1", "falla"))


class Cierre(unittest.TestCase):
    def test_webhook(self):
        self.assertEqual(ejecutor.esperar_webhook(1000, 1010, {"id": "b"}), "seguir")
        self.assertEqual(ejecutor.esperar_webhook(1000, 1119, None), "esperar")
        self.assertEqual(ejecutor.esperar_webhook(1000, 1120, None), "manual")

    def test_decidir(self):
        destruir = {"accion": "destruir", "resultado": "ok"}
        esperar = {"accion": "esperar_otro", "resultado": "esperando el cierre del otro -cd"}
        manual = {"accion": "manual", "resultado": "ok"}
        nada = {"accion": "manual", "resultado": "nada que destruir"}
        self.assertEqual(ejecutor.decidir_cierre([destruir], ["backend"]), "hecho")
        self.assertEqual(ejecutor.decidir_cierre([esperar, destruir], ["backend", "frontend"]), "hecho")
        self.assertEqual(ejecutor.decidir_cierre([esperar, esperar], ["backend", "frontend"]), "manual")
        self.assertEqual(ejecutor.decidir_cierre([esperar, esperar, manual], ["backend", "frontend"]), "hecho")
        self.assertEqual(ejecutor.decidir_cierre([esperar, esperar, nada], ["backend", "frontend"]), "hecho")
        self.assertEqual(ejecutor.decidir_cierre([{"accion": "destruir", "resultado": "fallo"}], ["backend"]), "falla")

    def test_resultado_fijo(self):
        ok = {"build_ok": True, "despliegue": "ok", "espera": {"resultado": "ok"}, "suite": {"resultado": "ok"}}
        self.assertEqual(ejecutor.resultado_fijo(ok), "ok")
        self.assertEqual(ejecutor.resultado_fijo(dict(ok, suite={"resultado": "fallo"})), "falla")
        self.assertEqual(ejecutor.resultado_fijo(dict(ok, espera=None)), "falla")


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class MergeTagJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        git(self.d, "init", "-q", "-b", "main")
        (self.d / "tag.json").write_text('{"tag": "base"}\n')
        (self.d / "README.md").write_text("base\n")
        git(self.d, "add", ".")
        git(self.d, "commit", "-q", "-m", "base")
        git(self.d, "checkout", "-q", "-b", "release/c1-1")
        (self.d / "tag.json").write_text('{"tag": "version", "rama": "release/c1-1"}\n')
        git(self.d, "commit", "-q", "-am", "version")
        git(self.d, "checkout", "-q", "main")
        git(self.d, "checkout", "-q", "-b", "dev")
        (self.d / "tag.json").write_text('{"tag": "otra"}\n')
        git(self.d, "commit", "-q", "-am", "otra version")
        git(self.d, "config", "user.name", "t")
        git(self.d, "config", "user.email", "t@t")

    def tearDown(self):
        self.tmp.cleanup()

    def test_conflicto_en_tag_json_se_resuelve_con_theirs(self):
        self.assertEqual(ejecutor.merge_con_tag(self.d, "release/c1-1", "integracion"), "tag.json")
        self.assertEqual(json.loads((self.d / "tag.json").read_text()), {"tag": "version", "rama": "release/c1-1"})
        self.assertEqual(git(self.d, "log", "-1", "--format=%P").count(" "), 1)  # commit de merge (dos padres)
        self.assertEqual(git(self.d, "status", "--porcelain"), "")

    def test_sin_conflicto_igual_hace_merge_commit(self):
        git(self.d, "checkout", "-q", "main")
        self.assertEqual(ejecutor.merge_con_tag(self.d, "release/c1-1", "integracion"), "sin_conflicto")
        self.assertEqual(git(self.d, "log", "-1", "--format=%s"), "integracion")

    def test_otro_conflicto_aborta(self):
        git(self.d, "checkout", "-q", "release/c1-1")
        (self.d / "README.md").write_text("version\n")
        git(self.d, "commit", "-q", "-am", "readme version")
        git(self.d, "checkout", "-q", "dev")
        (self.d / "README.md").write_text("dev\n")
        git(self.d, "commit", "-q", "-am", "readme dev")
        antes = git(self.d, "rev-parse", "HEAD")
        with self.assertRaises(ejecutor.ErrorEjecutor):
            ejecutor.merge_con_tag(self.d, "release/c1-1", "integracion")
        self.assertEqual(git(self.d, "rev-parse", "HEAD"), antes)
        self.assertEqual(git(self.d, "status", "--porcelain"), "")


class CorridaSimulada(unittest.TestCase):
    def test_c96_completa(self):
        e = escenario_c96()
        ej = ejecutor.Ejecutor(e, ejecutor.PlataformaSimulada(e, inicio=1_800_000_000.0), escribir=False)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ej.correr(), 0)
        r = ej.resumen()
        self.assertEqual(r["resultado"], "ok")
        por = {v["rama"]: v for v in r["versiones"]}
        for v in por.values():
            for m in ("efimero", "fijo"):
                self.assertEqual(v["modelos"][m]["validacion_final"]["resultado"], "ok")
                self.assertGreater(v["modelos"][m]["tiempo_validacion_seg"], 0)
            self.assertIsNotNone(v["limpieza"])
        self.assertEqual(por["release/c96-1"]["modelos"]["efimero"]["cierre"]["destruccion"]["via"], "webhook")
        self.assertEqual(por["release/c96-3"]["modelos"]["efimero"]["cierre"]["destruccion"]["via"], "manual")
        # c96-3 backend se integró dos veces en el fijo (apertura y actualización) y frontend una.
        integ = [(i["rama"], i["repo"], i["k"]) for i in r["integraciones_fijo"]]
        self.assertEqual([x for x in integ if x[:2] == ("release/c96-3", "backend")],
                         [("release/c96-3", "backend", 0), ("release/c96-3", "backend", 1)])
        self.assertEqual(integ.count(("release/c96-3", "frontend", 0)), 1)
        # Una validación del fijo por evento y cada integración es el commit del -cd de su evento (no la punta).
        self.assertEqual(len(r["integraciones_fijo"]), len(r["eventos"]))
        for i in r["integraciones_fijo"]:
            self.assertEqual(i["head"], r["eventos"][i["ev"]]["m"]["fijo"]["cd_sha"])
        # La final de c96-3 en el fijo valida el digest de la actualización de la rama del fijo.
        f = por["release/c96-3"]["modelos"]["fijo"]["validacion_final"]["por_repo"]["backend"]["build_id"]
        vals = {x["build_id"]: x for x in por["release/c96-3"]["modelos"]["fijo"]["validaciones"]}
        self.assertEqual(vals[f]["digest"], por["release/c96-3"]["digest_m"]["fijo"]["backend"])
        self.assertNotEqual(por["release/c96-3"]["digest_m"]["fijo"]["backend"], por["release/c96-3"]["digest_m"]["efimero"]["backend"])
        self.assertEqual(r["esquema"], 2)

    def test_aborta_si_espera_demasiado(self):
        e = escenario_c96()
        p = ejecutor.PlataformaSimulada(e, inicio=1_800_000_000.0)
        p.validacion = lambda modelo, repo, build_id: None  # nunca aparece un registro
        ej = ejecutor.Ejecutor(e, p, escribir=False)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ej.correr(), 2)
        self.assertEqual(ej.s["resultado"], "invalida")
        self.assertIn(f"{ejecutor.MAX_ESPERA_SEG // 60} min", ej.s["motivo"])
        self.assertEqual(ejecutor.MAX_ESPERA_SEG, 43200)


def v_exp(n, cierres=("pendiente", "pendiente"), expirada=None):
    return {"n": n, "expirada": expirada,
            "cierre": {"efimero": {"estado": cierres[0]}, "fijo": {"estado": cierres[1]}}}


class Expiracion(unittest.TestCase):
    EVENTOS = [{"n": 1, "despachado": 1000.0}, {"n": 1, "despachado": 1500.0}, {"n": 2, "despachado": 1900.0},
               {"n": 3, "despachado": None}]

    def test_ultimo_evento(self):
        self.assertEqual(ejecutor.ultimo_evento(self.EVENTOS, 1), 1500.0)
        self.assertIsNone(ejecutor.ultimo_evento(self.EVENTOS, 3))

    def test_seleccion(self):
        vs = [v_exp(1), v_exp(2), v_exp(3)]
        self.assertEqual(ejecutor.versiones_que_expiran(vs, self.EVENTOS, 2000.0, 400), [1])
        self.assertEqual(ejecutor.versiones_que_expiran(vs, self.EVENTOS, 2000.0, 500), [])  # 500 justo: no vence
        self.assertEqual(ejecutor.versiones_que_expiran(vs, self.EVENTOS, 2400.0, 400), [1, 2])

    def test_cerradas_o_ya_expiradas_no(self):
        vs = [v_exp(1, ("hecho", "hecho")), v_exp(2, ("en_curso", "hecho")), v_exp(3, expirada={"hora": "x"})]
        eventos = [{"n": 1, "despachado": 0.0}, {"n": 2, "despachado": 0.0}, {"n": 3, "despachado": 0.0}]
        self.assertEqual(ejecutor.versiones_que_expiran(vs, eventos, 10_000.0, 60), [])
        # abierta en un solo modelo: expira
        self.assertEqual(ejecutor.versiones_que_expiran([v_exp(4, ("hecho", "pendiente"))], [{"n": 4, "despachado": 0.0}],
                                                        10_000.0, 60), [4])


class CorridaSimuladaConExpiracion(unittest.TestCase):
    def correr(self, corrida, nivel, semilla, umbral, ajustar=None):
        e = escenario_c96() if (corrida, nivel, semilla) == (96, 3, 9603) else escenario.generar(corrida, nivel, semilla)
        if ajustar:
            ajustar(e)
        ej = ejecutor.Ejecutor(e, ejecutor.PlataformaSimulada(e, inicio=1_800_000_000.0), escribir=False, expiracion_seg=umbral)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ej.correr(), 0)
        return ej

    def test_c95_expira_despues_de_las_validaciones_en_curso(self):
        ej = self.correr(95, 1, 9503, 180)
        v = ej.V(1)
        self.assertEqual(v["resultado"], "integrada")
        self.assertEqual(v["resultado_cierre"], "declinada")
        self.assertEqual(v["expirada"]["modelos"], ["efimero", "fijo"])
        # esperó a que terminaran el despliegue efímero y el del fijo: las dos finales quedan con la marca
        self.assertEqual(sorted(v["expirada"]["finales_registradas"]), ["efimero", "fijo"])
        self.assertTrue(all(v["final"][m]["expirada"] for m in ("efimero", "fijo")))
        self.assertGreaterEqual(ejecutor.epoch(v["expirada"]["hora"]), max(v["final"][m]["end"] for m in ("efimero", "fijo")))
        self.assertEqual(v["cierre"]["efimero"]["repos"]["backend"]["accion_pr"], "close")
        self.assertEqual(v["cierre"]["fijo"]["repos"]["backend"]["accion_pr"], "close")
        self.assertEqual(v["cierre"]["efimero"]["destruccion"]["via"], "webhook")

    def test_combinacion_expira_antes_de_abrir_su_segundo_repo(self):
        # c96-3: backend abre en t=404 y actualiza en t=707. Se corre la apertura del frontend a t=5000 y con
        # 1200 s la versión vence (y sus builds ya terminaron) antes de esa apertura.
        def frontend_tarde(e):
            for ev in e["eventos_ordenados"] + e["versiones"][2]["eventos"]:
                if ev["repo"] == "frontend":
                    ev["t"] = 5000
        ej = self.correr(96, 3, 9603, 1200, frontend_tarde)
        v = ej.V(3)
        self.assertIsNotNone(v["expirada"])
        fe = [ev for ev in ej.eventos_de(3) if ev["repo"] == "frontend"][0]
        self.assertEqual(fe["estado"], "cancelado")
        self.assertIn(f"{fe['i']}-efimero", v["expirada"]["eventos_cancelados"])
        self.assertIn(f"{fe['i']}-fijo", v["expirada"]["eventos_cancelados"])
        self.assertEqual(v["cierre"]["efimero"]["repos"]["frontend"]["accion"], "sin_rama")
        self.assertEqual(v["cierre"]["efimero"]["destruccion"]["via"], "webhook")  # solo backend: destruye sin esperar al otro
        self.assertEqual(v["cierre"]["fijo"]["repos"]["frontend"]["accion_pr"], "sin_rama")
        self.assertNotIn("frontend", v["app_pr"]["efimero"])
        self.assertNotIn("frontend", v["app_pr"]["fijo"])
        self.assertIsNotNone(v["limpieza"])

    def test_sin_expiracion_por_defecto(self):
        ej = self.correr(96, 3, 9603, ejecutor.EXPIRACION_SEG)
        self.assertTrue(all(v["expirada"] is None for v in ej.s["versiones"].values()))


def correr_sim(e, plataforma=None, **kw):
    p = plataforma or ejecutor.PlataformaSimulada(e, inicio=1_800_000_000.0)
    ej = ejecutor.Ejecutor(e, p, escribir=False, **kw)
    with redirect_stdout(io.StringIO()):
        rc = ej.correr()
    return ej, rc


class ReglaDeActualizacion(unittest.TestCase):
    """La actualización k en cada modelo: después de que terminaron todas las validaciones de la versión en ese
    modelo, más la brecha del escenario."""

    def comprobar(self, e):
        ej, rc = correr_sim(e)
        self.assertEqual(rc, 0, ej.s["motivo"])
        for ev in ej.s["eventos"]:
            if ev["accion"] != "actualizacion":
                continue
            v = ej.V(ev["n"])
            for m in ejecutor.MODELOS:
                x = ev["m"][m]
                self.assertEqual(x["T"], x["F"] + ev["brecha"])
                self.assertGreaterEqual(x["despachado"], x["T"])
                self.assertLess(x["despachado"] - x["T"], ejecutor.VUELTA_SEG + 1)
                vals = v["validaciones"][m]
                # F es el fin de la última validación terminada antes de T, y no hay ninguna en curso al despachar
                self.assertEqual(x["F"], max(val["end"] for val in vals if val["end"] <= x["T"]))
                self.assertFalse([val for val in vals if val["start"] < x["despachado"] < val["end"]])
                prev = ej.s["eventos"][ev["previo"]]
                self.assertLessEqual(ej.validado(prev, m), x["F"])
        return ej

    def test_c96_y_c60(self):
        for c in (96, 60):
            self.comprobar(json.loads((RAIZ / "escenarios" / f"c{c}.json").read_text()))

    def test_combinaciones_esperan_a_backend_y_frontend(self):
        ej = self.comprobar(json.loads((RAIZ / "escenarios" / "c61.json").read_text()))
        combos = [v for v in ej.s["versiones"].values() if v["tipo"] == "combinacion"]
        self.assertTrue(any(len({x["repo"] for x in v["validaciones"]["efimero"]}) == 2 for v in combos))

    def test_cada_modelo_en_su_momento(self):
        ej = self.comprobar(json.loads((RAIZ / "escenarios" / "c60.json").read_text()))
        dist = [ev for ev in ej.s["eventos"] if ev["accion"] == "actualizacion" and ev["m"]["efimero"]["T"] != ev["m"]["fijo"]["T"]]
        self.assertTrue(dist)  # el fijo y el efímero reciben la misma actualización en momentos distintos

    def test_dos_builds_por_evento_con_imagen_propia(self):
        ej, _ = correr_sim(escenario_c96())
        bs = [b for proy, lista in ej.p.b.items() if proy.startswith("tesis-build-") for b in lista]
        self.assertEqual(len(bs), 2 * len(ej.s["eventos"]))
        for x in ej.s["eventos"]:
            e, f = x["m"]["efimero"], x["m"]["fijo"]
            self.assertNotEqual(e["commit"], f["commit"])
            self.assertNotEqual(e["digest"], f["digest"])
            self.assertEqual(e["arbol"], f["arbol"])
            self.assertEqual((e["tag"]["modelo"], f["tag"]["modelo"]), ("efimero", "fijo"))

    def test_cola_del_fijo_de_a_una_y_por_entrega(self):
        ej, _ = correr_sim(json.loads((RAIZ / "escenarios" / "c61.json").read_text()))
        fj = sorted((b["start"], b["end"]) for proy, lista in ej.p.b.items() if "fijo-desplegar" in proy for b in lista)
        self.assertTrue(all(a[1] <= b[0] for a, b in zip(fj, fj[1:])))  # nunca dos builds del fijo a la vez
        integ = ej.s["cola_fijo"]["integraciones"]
        for i in integ:  # al integrar, no había ninguna solicitud entregada antes esperando
            antes = [s for s in ej.s["solicitudes_fijo"] if ejecutor.epoch(s["listo"]) < ejecutor.epoch(i["listo"])
                     and ejecutor.epoch(s["listo"]) <= ejecutor.epoch(i["hora"]) and ejecutor.epoch(s["hora"]) > ejecutor.epoch(i["hora"])]
            self.assertEqual(antes, [])


class Plataforma2(ejecutor.PlataformaSimulada):
    """Variantes para probar los controles en vivo."""
    falla = None

    def _evento_app(self, n, repo, modelo, k, pr):
        commit = super()._evento_app(n, repo, modelo, k, pr)
        if self.falla == "duplicado" and (n, repo, modelo, k) == (1, "backend", "fijo", 0):
            proy = ejecutor.proyecto("unico", repo)
            orig = self.b[proy][0]
            dup = self._build(proy, self.t + 30, ejecutor.DUR["unico"], commit, orig["source"])  # redelivery
            dup["id"] = dup["id"].replace("-sim", "-dup")
        return commit

    def tag_json(self, cd, ref):
        t = super().tag_json(cd, ref)
        if self.falla == "modelo" and t and t.get("modelo") == "fijo":
            return dict(t, modelo="efimero")
        return t

    def validacion(self, modelo, repo, build_id):
        d = super().validacion(modelo, repo, build_id)
        if self.falla == "ajeno" and d and modelo == "fijo":
            otro = next(x for x in self.val.values() if x["rama"] == d["rama"] and x is not d and x["digest"] != d["digest"]
                        and any(x is self.val.get(b["id"]) for proy, bs in self.b.items() if "efimero-desplegar" in proy for b in bs))
            return dict(d, digest=otro["digest"])
        return d


class ControlesEnVivo(unittest.TestCase):
    def correr(self, falla):
        e = escenario_c96()
        p = Plataforma2(e, inicio=1_800_000_000.0)
        p.falla = falla
        return correr_sim(e, p)

    def test_duplicado_del_webhook_es_aviso(self):
        ej, rc = self.correr("duplicado")
        self.assertEqual(rc, 0, ej.s["motivo"])
        self.assertTrue(any("duplicados" in a for a in ej.s["avisos"]))
        x = ej.s["eventos"][0]["m"]["fijo"]
        self.assertEqual(len(x["duplicados"]) + 1, len(x["builds"]))

    def test_tag_json_de_otro_modelo_no_se_entrega(self):
        ej, rc = self.correr("modelo")
        self.assertEqual(rc, 2)
        self.assertIn("modelo equivocado", ej.s["motivo"])
        self.assertEqual(ej.s["solicitudes_fijo"], [])

    def test_validacion_con_digest_de_otro_modelo_aborta(self):
        ej, rc = self.correr("ajeno")
        self.assertEqual(rc, 2)
        self.assertIn("modelo equivocado", ej.s["motivo"])


if __name__ == "__main__":
    unittest.main()
