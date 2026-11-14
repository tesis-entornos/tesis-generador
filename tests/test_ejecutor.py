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


def pr(cd, number, created, sha="s"):
    return {"cd": cd, "number": number, "created": created, "sha": sha, "head": f"release/c1-{number}"}


class Cola(unittest.TestCase):
    def test_el_mas_antiguo(self):
        prs = [pr("tesis-backend-cd", 2, 200), pr("tesis-frontend-cd", 1, 150), pr("tesis-backend-cd", 3, 300)]
        self.assertEqual(ejecutor.elegir_pr_cola(prs, {})["number"], 1)

    def test_salta_las_puntas_ya_integradas(self):
        prs = [pr("tesis-backend-cd", 2, 100, "a"), pr("tesis-backend-cd", 3, 200, "b")]
        self.assertEqual(ejecutor.elegir_pr_cola(prs, {"tesis-backend-cd#2": ["a"]})["number"], 3)

    def test_punta_nueva_del_mismo_pr_vuelve_a_la_cola(self):
        prs = [pr("tesis-backend-cd", 2, 100, "b")]
        self.assertEqual(ejecutor.elegir_pr_cola(prs, {"tesis-backend-cd#2": ["a"]})["number"], 2)

    def test_vacia(self):
        self.assertIsNone(ejecutor.elegir_pr_cola([], {}))
        self.assertIsNone(ejecutor.elegir_pr_cola([pr("tesis-backend-cd", 2, 1, "a")], {"tesis-backend-cd#2": ["a"]}))


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
        e = escenario.generar(96, 3, 9603)
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
        integ = [(i["rama"], i["repo"]) for i in r["integraciones_fijo"]]
        self.assertEqual(integ.count(("release/c96-3", "backend")), 2)
        self.assertEqual(integ.count(("release/c96-3", "frontend")), 1)
        # La final de c96-3 en el fijo valida el digest de la actualización.
        f = por["release/c96-3"]["modelos"]["fijo"]["validacion_final"]["por_repo"]["backend"]["build_id"]
        vals = {x["build_id"]: x for x in por["release/c96-3"]["modelos"]["fijo"]["validaciones"]}
        self.assertEqual(vals[f]["digest"], por["release/c96-3"]["digest"]["backend"])

    def test_aborta_si_espera_demasiado(self):
        e = escenario.generar(96, 3, 9603)
        p = ejecutor.PlataformaSimulada(e, inicio=1_800_000_000.0)
        p.validacion = lambda modelo, repo, build_id: None  # nunca aparece un registro
        ej = ejecutor.Ejecutor(e, p, escribir=False)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ej.correr(), 2)
        self.assertEqual(ej.s["resultado"], "invalida")
        self.assertIn("90 min", ej.s["motivo"])


if __name__ == "__main__":
    unittest.main()
