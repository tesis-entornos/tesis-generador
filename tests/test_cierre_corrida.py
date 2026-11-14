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
            "resultado": resultado, "motivo": None if resultado == "ok" else "espera de más de 90 min",
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


if __name__ == "__main__":
    unittest.main()
