import csv
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import cierre_corrida as cc  # noqa: E402


def escenario(eventos=2):
    return {"corrida": 7, "eventos_ordenados": [{"n": 1, "t": 0}, {"n": 2, "t": 10}][:eventos]}


def version(n, tiempo_e, tiempo_f, expirada=None, finales=True):
    def modelo(m, tiempo, builds):
        f = {"build_id": builds[-1], "end": f"2026-01-01T00:{10 + n:02d}:00Z", "resultado": "ok"} if finales else None
        d = {"validacion_final": f, "tiempo_validacion_seg": tiempo if finales else None,
             "validaciones": [{"build_id": b} for b in builds]}
        if m == "fijo":
            d["integraciones"] = [{"pr_created_at": "2026-01-01T00:00:00Z", "hora": "2026-01-01T00:01:40Z", "head": f"h{n}"}]
        return d
    return {"n": n, "rama": f"release/c7-{n}", "tipo": "migracion", "resultado_escenario": "integrada",
            "apertura": "2026-01-01T00:00:00Z", "expirada": expirada,
            "modelos": {"efimero": modelo("efimero", tiempo_e, [f"e:{n}a", f"e:{n}b"]),
                        "fijo": modelo("fijo", tiempo_f, [f"f:{n}a"])}}


def corrida(versiones=None, resultado="ok", eventos=None):
    return {"corrida": "c7", "nivel": 3, "semilla": 701, "t0": "2026-01-01T00:00:00Z", "fin": "2026-01-01T01:00:00Z",
            "resultado": resultado, "motivo": None if resultado == "ok" else "espera de más de 90 min", "protocolo": "f" * 64,
            "eventos": eventos if eventos is not None else [
                {"n": 1, "repo": "backend", "t": 0, "estado": "unico_ok", "unico": {"status": "SUCCEEDED", "build_id": "u1"}},
                {"n": 2, "repo": "backend", "t": 10, "estado": "unico_ok", "unico": {"status": "SUCCEEDED", "build_id": "u2"}}],
            "versiones": versiones if versiones is not None else [version(1, 600, 400), version(2, 700, 500)]}


DEP_OK = {"encontrados": []}


class Validez(unittest.TestCase):
    def test_valida(self):
        self.assertEqual(cc.evaluar_validez(corrida(), escenario(), {}, {}, DEP_OK), [])

    def test_abortada(self):
        m = cc.evaluar_validez(corrida(resultado="invalida"), escenario(), {}, {}, DEP_OK)
        self.assertTrue(any("abortada" in x for x in m))

    def test_evento_sin_despachar_y_unico_fallido(self):
        ev = [{"n": 1, "repo": "backend", "t": 0, "estado": "unico_falla", "unico": {"status": "FAILED", "build_id": "u1"}},
              {"n": 2, "repo": "backend", "t": 10, "estado": "pendiente"}]
        m = cc.evaluar_validez(corrida(eventos=ev), escenario(), {}, {}, DEP_OK)
        self.assertTrue(any("sin despachar" in x for x in m))
        self.assertTrue(any("no SUCCEEDED" in x and "FAILED" in x for x in m))

    def test_cancelado_por_expiracion_cuenta(self):
        ev = corrida()["eventos"][:1] + [{"n": 2, "repo": "backend", "t": 10, "estado": "cancelado"}]
        vs = [version(1, 600, 400), version(2, None, None, expirada={"hora": "x"}, finales=False)]
        self.assertEqual(cc.evaluar_validez(corrida(versiones=vs, eventos=ev), escenario(), {}, {}, DEP_OK), [])

    def test_falta_final_sin_expiracion(self):
        vs = [version(1, 600, 400), version(2, None, None, finales=False)]
        m = cc.evaluar_validez(corrida(versiones=vs), escenario(), {}, {}, DEP_OK)
        self.assertEqual(len([x for x in m if "sin validación final" in x]), 2)

    def test_protocolo_no_verificado(self):
        for valor, texto in (("no verificado", "no verificado"), (None, "previa al protocolo")):
            c = corrida()
            c["protocolo"] = valor
            m = cc.evaluar_validez(c, escenario(), {}, {}, DEP_OK)
            self.assertEqual(len(m), 1)
            self.assertIn(texto, m[0])

    def test_restos_del_depurador(self):
        m = cc.evaluar_validez(corrida(), escenario(), {}, {}, {"encontrados": [{"tipo": "rama"}, {"tipo": "pr"}]})
        self.assertTrue(any("2 restos" in x for x in m))
        self.assertTrue(any("sin informe" in x for x in cc.evaluar_validez(corrida(), escenario(), {}, {}, None)))

    def test_proveedor(self):
        docs = {"e:1a": {"aprovisionamiento": {"resultado": "fallo", "motivo": "aprovisionamiento - terraform apply"}},
                "e:2a": {"aprovisionamiento": {"resultado": "fallo", "motivo": "otro error"}},
                "e:2b": {"aprovisionamiento": {"resultado": "ok", "motivo": "nada que ver con quota"}}}
        logs = {"e:1a": "Error: creating ECS Service: ... ThrottlingException: Rate exceeded"}
        m = cc.evaluar_validez(corrida(), escenario(), docs, logs, DEP_OK)
        self.assertEqual([x for x in m if "proveedor" in x], ["inválida por proveedor: aprovisionamiento fallido por cuota o límite en e:1a"])
        self.assertTrue(cc.falla_proveedor({"aprovisionamiento": {"resultado": "fallo", "x": "LimitExceeded"}}))
        self.assertFalse(cc.falla_proveedor({"aprovisionamiento": {"resultado": "ok", "x": "LimitExceeded"}}))


class Filas(unittest.TestCase):
    def test_percentiles(self):
        self.assertEqual(cc.percentiles([]), (None, None))
        self.assertEqual(cc.percentiles([500]), (500, 500))
        med, p90 = cc.percentiles([740.3, 781.5, 1228.5])
        self.assertEqual(med, 781.5)
        self.assertAlmostEqual(p90, 1139.1, places=6)
        self.assertAlmostEqual(cc.percentiles([100, 200])[1], 190.0)

    def test_filas_versiones(self):
        docs = {"e:1a": {"suite": {"stats": {"flaky": 1, "unexpected": 0}},
                         "aprovisionamiento": {"creado": True, "crear_entorno": {"total_seg": 303}}},
                "e:1b": {"suite": {"stats": {"flaky": 2, "unexpected": 1}}, "aprovisionamiento": {"creado": False}},
                "f:1a": {"suite": {"stats": {"flaky": 0, "unexpected": 0}}}}
        filas = cc.filas_versiones(corrida(), escenario(), docs, {"h1": cc.epoch("2026-01-01T00:00:30Z")})
        e1 = next(f for f in filas if f["n"] == 1 and f["modelo"] == "efimero")
        f1 = next(f for f in filas if f["n"] == 1 and f["modelo"] == "fijo")
        self.assertEqual((e1["validaciones"], e1["flaky"], e1["unexpected"], e1["aprovisionamiento_s"], e1["espera_fijo_s"]),
                         (2, 3, 1, 303, None))
        self.assertEqual((f1["espera_fijo_s"], f1["aprovisionamiento_s"]), (70.0, None))  # desde la punta (00:00:30)
        f2 = next(f for f in filas if f["n"] == 2 and f["modelo"] == "fijo")
        self.assertEqual(f2["espera_fijo_s"], 100.0)  # sin fecha de punta: desde el createdAt
        self.assertEqual(len(filas), 4)

    def test_filas_corridas(self):
        filas_v = cc.filas_versiones(corrida(), escenario(), {})
        fc = {f["modelo"]: f for f in cc.filas_corridas(corrida(), filas_v, [])}
        self.assertEqual(fc["efimero"]["versiones"], 2)
        self.assertEqual(fc["efimero"]["mediana_tiempo_validacion_s"], 650.0)
        self.assertEqual(fc["efimero"]["p90_tiempo_validacion_s"], 690.0)
        # último fin de validación final: 00:12:00 -> 2 / 720 s * 3600
        self.assertEqual(fc["fijo"]["versiones_por_hora"], 10.0)
        self.assertEqual(fc["fijo"]["espera_acumulada_s"], 200.0)
        self.assertIsNone(fc["efimero"]["espera_acumulada_s"])
        self.assertTrue(fc["fijo"]["valida"])
        self.assertEqual(fc["fijo"]["protocolo"], "f" * 64)
        fc = {f["modelo"]: f for f in cc.filas_corridas(corrida(), filas_v, ["x", "y"])}
        self.assertEqual((fc["fijo"]["valida"], fc["fijo"]["motivo_invalidez"]), (False, "x; y"))

    def test_expiradas_no_entran_en_las_estadisticas(self):
        vs = [version(1, 600, 400), version(2, 900, 800, expirada={"hora": "x"})]
        filas_v = cc.filas_versiones(corrida(versiones=vs), escenario(), {})
        self.assertTrue(next(f for f in filas_v if f["n"] == 2)["expirada"])
        fc = {f["modelo"]: f for f in cc.filas_corridas(corrida(versiones=vs), filas_v, [])}
        self.assertEqual((fc["efimero"]["versiones"], fc["efimero"]["mediana_tiempo_validacion_s"]), (1, 600))

    def test_csv_reemplaza_las_filas_de_la_corrida(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "x.csv"
            cols = ["corrida", "n", "modelo", "valor"]
            cc.actualizar_csv(ruta, cols, [{"corrida": "c10", "n": 1, "modelo": "fijo", "valor": 1}], "c10")
            cc.actualizar_csv(ruta, cols, [{"corrida": "c9", "n": 1, "modelo": "fijo", "valor": True}], "c9")
            cc.actualizar_csv(ruta, cols, [{"corrida": "c10", "n": 1, "modelo": "fijo", "valor": 2},
                                           {"corrida": "c10", "n": 1, "modelo": "efimero", "valor": None}], "c10")
            filas = list(csv.DictReader(ruta.open()))
            self.assertEqual([(f["corrida"], f["modelo"], f["valor"]) for f in filas],
                             [("c9", "fijo", "true"), ("c10", "efimero", ""), ("c10", "fijo", "2")])


def corrida_dos_ramas(c=96):
    """Registro (esquema 2) de una corrida simulada completa, como lo escribe el ejecutor."""
    import io
    import json
    from contextlib import redirect_stdout
    import ejecutor
    e = json.loads((RAIZ / "escenarios" / f"c{c}.json").read_text())
    ej = ejecutor.Ejecutor(e, ejecutor.PlataformaSimulada(e, inicio=1_800_000_000.0), escribir=False)
    with redirect_stdout(io.StringIO()):
        assert ej.correr() == 0
    r = json.loads(json.dumps(ej.resumen()))
    r["protocolo"] = "f" * 64
    return r, e


class DosRamas(unittest.TestCase):
    def setUp(self):
        self.r, self.e = corrida_dos_ramas()

    def motivos(self):
        return cc.controles_dos_ramas(self.r)["motivos"]

    def evento(self, n, repo, accion="actualizacion"):
        return next(ev for ev in self.r["eventos"] if ev["n"] == n and ev["repo"] == repo and ev["accion"] == accion)

    def version(self, n):
        return next(v for v in self.r["versiones"] if v["n"] == n)

    def test_limpia_y_valida(self):
        self.assertEqual(cc.controles_dos_ramas(self.r), {"motivos": [], "avisos": []})
        self.assertEqual(cc.evaluar_validez(self.r, self.e, {}, {}, DEP_OK), [])

    def test_entrega_al_modelo_equivocado(self):
        v = self.version(3)
        v["modelos"]["fijo"]["validaciones"][0]["digest"] = v["digest_m"]["efimero"]["backend"]
        m = self.motivos()
        self.assertTrue(any("modelo equivocado" in x and "del fijo desplegó una imagen del efimero" in x for x in m))
        self.assertTrue(any("modelo equivocado" in x for x in cc.evaluar_validez(self.r, self.e, {}, {}, DEP_OK)))

    def test_historial_del_cd_con_commit_de_otro_modelo(self):
        v = self.version(1)
        hist = v["historial_cd"]["tesis-backend-cd"]
        hist["release/c96-1"][0]["modelo"] = "fijo"
        self.assertTrue(any("modelo equivocado" in x and "release/c96-1" in x for x in self.motivos()))

    def test_tag_json_de_otro_modelo(self):
        self.evento(1, "backend", "apertura")["m"]["fijo"]["tag"]["modelo"] = "efimero"
        self.assertTrue(any("modelo equivocado" in x and "tag.json" in x for x in self.motivos()))

    def test_builds_duplicados_es_aviso(self):
        x = self.evento(1, "backend", "apertura")["m"]["fijo"]
        x["duplicados"] = [{"build_id": "tesis-build-backend:dup", "status": "SUCCEEDED"}]
        x["builds"].append({"id": "tesis-build-backend:dup", "status": "SUCCEEDED"})
        r = cc.controles_dos_ramas(self.r)
        self.assertEqual(r["motivos"], [])
        self.assertTrue(any("duplicados" in a for a in r["avisos"]))

    def test_escritor_que_no_esta_entre_sus_builds(self):
        self.evento(1, "backend", "apertura")["m"]["fijo"]["unico"]["build_id"] = "otro"
        self.assertTrue(any("builds duplicados" in x for x in self.motivos()))

    def test_colision_de_tag(self):
        a = self.evento(1, "backend", "apertura")["m"]["fijo"]
        b = self.evento(2, "backend", "apertura")["m"]["efimero"]
        a["tag"]["tag"] = b["tag"]["tag"]
        self.assertTrue(any("colisión de tag" in x for x in self.motivos()))

    def test_misma_imagen_en_los_dos_modelos(self):
        ev = self.evento(1, "backend", "apertura")
        ev["m"]["fijo"]["digest"] = ev["m"]["efimero"]["digest"]
        self.assertTrue(any("misma imagen" in x for x in self.motivos()))

    def test_imagen_subida_fuera_de_su_build(self):
        x = self.evento(1, "backend", "apertura")["m"]["efimero"]
        x["imagen"]["pushed_at"] = x["unico"]["start"] - 3600
        self.assertTrue(any("fuera de su build" in x for x in self.motivos()))

    def test_arboles_distintos(self):
        self.evento(1, "backend", "apertura")["m"]["fijo"]["arbol"] = "0" * 40
        self.assertTrue(any("mismo árbol" in x for x in self.motivos()))

    def test_un_modelo_recibe_y_el_otro_no(self):
        v = self.version(3)
        d = self.evento(3, "backend")["m"]["fijo"]["digest"]
        v["modelos"]["fijo"]["validaciones"] = [x for x in v["modelos"]["fijo"]["validaciones"] if x["digest"] != d]
        self.assertTrue(any("un modelo recibe y el otro no" in x and "sin validación" in x for x in self.motivos()))
        self.evento(2, "backend", "apertura")["m"]["efimero"]["estado"] = "despachado"
        self.assertTrue(any("un modelo recibe y el otro no" in x and "c96-2" in x for x in self.motivos()))

    def test_actualizacion_antes_de_su_T(self):
        x = self.evento(3, "backend")["m"]["efimero"]
        x["despachado"] = x["T"] - 30
        self.assertTrue(any("fuera de orden" in m and "antes de T" in m for m in self.motivos()))

    def test_actualizacion_con_validacion_en_curso(self):
        x = self.evento(3, "backend")["m"]["fijo"]
        previa = next(v for v in self.version(3)["modelos"]["fijo"]["validaciones"] if v["repo"] == "backend")
        x["T"] = x["despachado"] = cc.epoch(previa["start"]) + 60
        x["F"] = x["T"] - self.evento(3, "backend")["brecha"]
        self.assertTrue(any("fuera de orden" in m and "en curso" in m for m in self.motivos()))

    def test_integracion_del_fijo_fuera_de_orden(self):
        integ = [i for i in self.r["integraciones_fijo"] if i["rama"] == "release/c96-3" and i["repo"] == "backend"]
        integ[0]["k"], integ[1]["k"] = integ[1]["k"], integ[0]["k"]
        self.assertTrue(any("fuera de orden" in m and "integró" in m for m in self.motivos()))

    def test_filas_desde_la_entrega_y_la_rama_del_modelo(self):
        filas = {(f["n"], f["modelo"]): f for f in cc.filas_versiones(self.r, self.e, {})}
        v = self.version(3)
        self.assertEqual(filas[(3, "fijo")]["t_apertura"], v["apertura_m"]["fijo"])
        esperada = round(sum(cc.epoch(i["hora"]) - cc.epoch(i["listo"]) for i in v["modelos"]["fijo"]["integraciones"]), 1)
        self.assertEqual(filas[(3, "fijo")]["espera_fijo_s"], esperada)
        self.assertEqual(filas[(3, "efimero")]["validaciones"], 3)
        self.assertEqual(filas[(3, "fijo")]["validaciones"], 3)

    def test_inicio_solicitud_con_listo(self):
        self.assertEqual(cc.inicio_solicitud({"listo": "2026-01-01T00:00:10Z", "pr_created_at": "2026-01-01T00:00:00Z"}, {}),
                         cc.epoch("2026-01-01T00:00:10Z"))


C60_CRUDOS = Path.home() / "tesis/aplicacion/piloto-definitivo-c60/analisis/crudos"


@unittest.skipUnless(C60_CRUDOS.exists(), "sin los crudos de c60")
class EsquemaUnoSinCambios(unittest.TestCase):
    def test_c60_da_las_mismas_filas_que_datos_versiones_csv(self):
        import json
        corrida = json.loads((RAIZ / "registros" / "corrida-c60.json").read_text())
        escenario = json.loads((RAIZ / "escenarios" / "c60.json").read_text())
        docs = {}
        for m in cc.MODELOS:
            for f in (C60_CRUDOS / "s3" / "c60" / m).glob("*/validacion.json"):
                docs[f"tesis-{m}-desplegar-{json.loads(f.read_text())['servicio']}:{f.parent.name}"] = json.loads(f.read_text())
        fechas = {}
        for f in (C60_CRUDOS / "github").glob("*/*.json"):
            fechas[f.stem] = cc.epoch(json.loads(f.read_text())["commit"]["committer"]["date"])
        filas = cc.filas_versiones(corrida, escenario, docs, fechas)
        with tempfile.TemporaryDirectory() as d:
            cc.actualizar_csv(Path(d) / "v.csv", cc.COLUMNAS_VERSIONES, filas, "c60")
            nuevas = (Path(d) / "v.csv").read_text().splitlines()[1:]
        viejas = [x for x in (RAIZ / "datos" / "versiones.csv").read_text().splitlines() if x.startswith("c60,")]
        self.assertEqual(nuevas, viejas)


if __name__ == "__main__":
    unittest.main()
