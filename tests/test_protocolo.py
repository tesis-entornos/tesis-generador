import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import protocolo  # noqa: E402

SUITE = "582621604637.dkr.ecr.us-east-2.amazonaws.com/tesis-playwright@sha256:" + "2" * 64
TF = "47032ae0df2c7b9ccbd9b7930565a157ef535ed1"


def manifiesto():
    m = {
        "nombre": "protocolo-v1", "fecha": "2026-11-14T00:00:00Z",
        "buildspecs": {"bucket": "b", "prefijo": "main/", "etags": {"desplegar-efimero.yml": "a" * 32, "scripts/esperar-entorno.sh": "b" * 32}},
        "tag_json_main": {"tesis-backend-cd": {"digest": "sha256:" + "e" * 64}, "tesis-frontend-cd": {"digest": "sha256:" + "f" * 64}},
        "suite": {"imagen": SUITE, "playwright_retries": 2},
        "simular_trabajo_seg": {"valor_publicado": "600"},
        "terraform": {"fijado": TF},
        "generador": {"commit": "c" * 40},
        "escenario": protocolo.parametros_escenario(),
    }
    m["huella"] = protocolo.huella(m)
    return m


def vivo(m):
    return {"etags": dict(m["buildspecs"]["etags"]),
            "suite": {"imagenes": {"desplegar-efimero.yml": SUITE, "desplegar-fijo.yml": SUITE}, "playwright_retries": 2,
                      "simular_trabajo_seg": {"desplegar-efimero.yml": "600", "desplegar-fijo.yml": "600"},
                      "sleeps_simular": {"desplegar-efimero.yml": 1, "desplegar-fijo.yml": 1}},
            "pisadas": [], "terraform": {p: TF for p in protocolo.PROYECTOS_EFIMERO},
            "tag_json_main": copy.deepcopy(m["tag_json_main"]), "escenario": protocolo.parametros_escenario(),
            "escenario_archivo": protocolo.parametros_escenario()["parametros"], "generador": []}


class Comparar(unittest.TestCase):
    def test_coincide(self):
        m = manifiesto()
        self.assertEqual(protocolo.comparar(m, vivo(m)), [])

    def test_etag_distinto(self):
        m = manifiesto()
        v = vivo(m)
        v["etags"]["scripts/esperar-entorno.sh"] = "9" * 32
        d = protocolo.comparar(m, v)
        self.assertEqual(len(d), 1)
        self.assertIn("main/scripts/esperar-entorno.sh", d[0])

    def test_objeto_de_mas_o_de_menos(self):
        m = manifiesto()
        v = vivo(m)
        del v["etags"]["desplegar-efimero.yml"]
        v["etags"]["nuevo.sh"] = "1" * 32
        self.assertEqual(len(protocolo.comparar(m, v)), 2)

    def test_digest_de_la_suite_distinto(self):
        m = manifiesto()
        v = vivo(m)
        v["suite"]["imagenes"]["desplegar-fijo.yml"] = SUITE[:-1] + "3"
        d = protocolo.comparar(m, v)
        self.assertEqual(len(d), 1)
        self.assertIn("desplegar-fijo.yml", d[0])

    def test_retries_simular_y_variables_de_proyecto(self):
        m = manifiesto()
        v = vivo(m)
        v["suite"]["playwright_retries"] = 0
        v["suite"]["simular_trabajo_seg"] = {"desplegar-efimero.yml": "120", "desplegar-fijo.yml": "120"}
        v["pisadas"] = [{"proyecto": "tesis-efimero-desplegar-backend", "variable": "SIMULAR_TRABAJO_SEG", "valor": "60"}]
        self.assertEqual(len(protocolo.comparar(m, v)), 3)

    def test_simular_distinto_entre_buildspecs(self):
        m = manifiesto()
        v = vivo(m)
        v["suite"]["simular_trabajo_seg"]["desplegar-fijo.yml"] = "0"
        d = protocolo.comparar(m, v)
        self.assertEqual(len(d), 1)
        self.assertIn("distinto entre buildspecs", d[0])
        v["suite"]["simular_trabajo_seg"]["desplegar-efimero.yml"] = "0"  # iguales entre sí, pero no los del manifiesto
        d = protocolo.comparar(m, v)
        self.assertEqual(len(d), 1)
        self.assertIn("manifiesto '600'", d[0])

    def test_sleeps_de_simular(self):
        m = manifiesto()
        v = vivo(m)
        v["suite"]["sleeps_simular"] = {"desplegar-efimero.yml": 2, "desplegar-fijo.yml": 0}
        d = protocolo.comparar(m, v)
        self.assertEqual(len(d), 2)
        self.assertIn("desplegar-efimero.yml tiene 2 sleep", d[0])
        self.assertIn("desplegar-fijo.yml tiene 0 sleep", d[1])

    def test_tag_json_terraform_y_escenario(self):
        m = manifiesto()
        v = vivo(m)
        v["tag_json_main"]["tesis-frontend-cd"] = {"digest": "sha256:" + "0" * 64}
        v["terraform"]["tesis-efimero-cerrar-frontend"] = "cec2c7f3835bb569d0a90d49f8cdddccf5acd8c3"
        v["escenario_archivo"] = dict(v["escenario_archivo"], resultado={"integrada": 0.5, "declinada": 0.5})
        self.assertEqual(len(protocolo.comparar(m, v)), 3)


WEBHOOK_BUILD = {"buildType": "BUILD", "status": "ACTIVE", "pullRequestBuildPolicy": {"requiresCommentApproval": "DISABLED"},
                 "filterGroups": [[{"type": "EVENT", "pattern": "PULL_REQUEST_CREATED,PULL_REQUEST_UPDATED,PULL_REQUEST_REOPENED",
                                    "excludeMatchedPattern": False},
                                   {"type": "BASE_REF", "pattern": "^refs/heads/main$", "excludeMatchedPattern": False},
                                   {"type": "HEAD_REF", "pattern": "^refs/heads/release/(efimero|fijo)/c[0-9]+-[0-9]+$",
                                    "excludeMatchedPattern": False}]]}


class DosRamas(unittest.TestCase):
    def manifiesto_v4(self):
        m = manifiesto()
        m["webhooks"] = {"tesis-build-backend": copy.deepcopy(WEBHOOK_BUILD), "tesis-fijo-desplegar-backend": None}
        m["ejecutor"] = protocolo.parametros_ejecutor()
        m["huella"] = protocolo.huella(m)
        return m

    def vivo_v4(self, m):
        v = vivo(m)
        v["webhooks"] = copy.deepcopy(m["webhooks"])
        v["ejecutor"] = protocolo.parametros_ejecutor()
        return v

    def test_coincide(self):
        m = self.manifiesto_v4()
        self.assertEqual(protocolo.comparar(m, self.vivo_v4(m)), [])

    def test_filtro_de_webhook_distinto_o_faltante(self):
        m = self.manifiesto_v4()
        v = self.vivo_v4(m)
        v["webhooks"]["tesis-build-backend"]["filterGroups"][0][2]["pattern"] = "^refs/heads/(release|hotfix)/.+$"
        v["webhooks"]["tesis-efimero-desplegar-backend"] = {"buildType": "BUILD"}
        d = protocolo.comparar(m, v)
        self.assertEqual(len(d), 2)
        self.assertTrue(any("tesis-build-backend" in x for x in d))
        self.assertTrue(any("tesis-efimero-desplegar-backend" in x for x in d))

    def test_parametros_del_ejecutor(self):
        m = self.manifiesto_v4()
        v = self.vivo_v4(m)
        v["ejecutor"] = dict(v["ejecutor"], max_espera_seg=14400)
        self.assertEqual(len(protocolo.comparar(m, v)), 1)
        self.assertEqual(protocolo.parametros_ejecutor()["max_espera_seg"], 21600)
        self.assertEqual(protocolo.parametros_ejecutor()["esquema"], 2)

    def test_manifiesto_sin_las_secciones_nuevas_no_las_compara(self):
        m = manifiesto()
        v = vivo(m)
        v["webhooks"] = {"x": {"buildType": "BUILD"}}
        self.assertEqual(protocolo.comparar(m, v), [])

    def test_webhook_sin_url_ni_secreto(self):
        p = {"name": "x", "webhook": dict(WEBHOOK_BUILD, url="https://u", payloadUrl="https://p", secret="s",
                                          lastModifiedSecret="2026", manualCreation=False)}
        self.assertEqual(protocolo.webhook_de(p), WEBHOOK_BUILD)
        self.assertIsNone(protocolo.webhook_de({"name": "y"}))

    def test_manifiesto_por_defecto(self):
        self.assertEqual(protocolo.MANIFIESTO.name, "protocolo-v4.json")


class Huella(unittest.TestCase):
    def test_estable_e_independiente_del_orden(self):
        m = manifiesto()
        reordenado = dict(reversed(list(m.items())))
        self.assertEqual(protocolo.huella(m), protocolo.huella(reordenado))
        self.assertEqual(protocolo.huella(m), m["huella"])

    def test_cambia_con_el_contenido(self):
        m = manifiesto()
        m["suite"]["playwright_retries"] = 3
        self.assertNotEqual(protocolo.huella(m), m["huella"])

    def test_suite_de_lo_publicado(self):
        sleep = '      - if [ "$SIMULAR_TRABAJO_SEG" -gt 0 ]; then echo "duerme"; sleep "$SIMULAR_TRABAJO_SEG"; fi\n'
        pub = {"desplegar-efimero.yml": f"env:\n  variables:\n    SIMULAR_TRABAJO_SEG: \"600\"\n    SUITE_IMAGEN: {SUITE}\n" + sleep,
               "desplegar-fijo.yml": f"    SIMULAR_TRABAJO_SEG: \"600\"\n    SUITE_IMAGEN: {SUITE}\n" + sleep,
               "scripts/ejecutar-suite.sh": "docker run -e PLAYWRIGHT_RETRIES=2 \\\n"}
        s = protocolo.suite_de(pub)
        self.assertEqual(s, {"imagenes": {"desplegar-efimero.yml": SUITE, "desplegar-fijo.yml": SUITE},
                             "playwright_retries": 2,
                             "simular_trabajo_seg": {"desplegar-efimero.yml": "600", "desplegar-fijo.yml": "600"},
                             "sleeps_simular": {"desplegar-efimero.yml": 1, "desplegar-fijo.yml": 1}})
        self.assertEqual(protocolo.problemas_simular(s, "600"), [])

    def test_suite_de_rechaza_valores_distintos_y_sleeps_de_mas_o_de_menos(self):
        sleep = '      - sleep "$SIMULAR_TRABAJO_SEG"\n'
        pub = {"desplegar-efimero.yml": "    SIMULAR_TRABAJO_SEG: \"600\"\n" + sleep + sleep,  # dos sleep
               "desplegar-fijo.yml": "    SIMULAR_TRABAJO_SEG: \"300\"\n      - sleep 600\n"}  # ninguno de SIMULAR
        s = protocolo.suite_de(pub)
        self.assertEqual(s["sleeps_simular"], {"desplegar-efimero.yml": 2, "desplegar-fijo.yml": 0})
        d = protocolo.problemas_simular(s, "600")
        self.assertEqual(len(d), 3)
        self.assertIn("distinto entre buildspecs", d[0])
        self.assertIn("desplegar-efimero.yml tiene 2 sleep", d[1])
        self.assertIn("desplegar-fijo.yml tiene 0 sleep", d[2])


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class Generador(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        git(self.d, "init", "-q", "-b", "main")
        for f in ("escenario.py", "acciones.py", "ejecutor.py", "datos/corridas.csv", "protocolo/protocolo-v1.json"):
            (self.d / f).parent.mkdir(parents=True, exist_ok=True)
            (self.d / f).write_text("x = 1\n")
        git(self.d, "add", ".")
        git(self.d, "commit", "-q", "-m", "base")
        self.base = git(self.d, "rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, archivos, mensaje="cambio"):
        for f, texto in archivos.items():
            (self.d / f).parent.mkdir(parents=True, exist_ok=True)
            (self.d / f).write_text(texto)
        git(self.d, "add", ".")
        git(self.d, "commit", "-q", "-m", mensaje)

    def test_mismo_commit(self):
        self.assertEqual(protocolo.verificar_generador(self.base, self.d), [])

    def test_descendiente_solo_con_registros_datos_escenarios_y_manifiesto(self):
        self.commit({"registros/c1.log": "log\n", "datos/corridas.csv": "x = 2\n", "escenarios/c1.json": "{}\n",
                     "datos/pruebas/versiones.csv": "a\n", "protocolo/protocolo-v1.json": "{}\n"})
        self.assertEqual(protocolo.verificar_generador(self.base, self.d), [])

    def test_descendiente_con_cambios_de_codigo(self):
        for f in ("ejecutor.py", "escenario.py", "acciones.py", "tests/test_x.py", "protocolo/otro.json", "README.md"):
            with self.subTest(f=f):
                self.setUp()
                self.commit({"datos/corridas.csv": "x = 2\n", f: "x = 9\n"})
                d = protocolo.verificar_generador(self.base, self.d)
                self.assertEqual(len(d), 1)
                self.assertIn(f, d[0])
                self.assertNotIn("datos/corridas.csv", d[0])

    def test_cambio_local_sin_commitear(self):
        (self.d / "ejecutor.py").write_text("x = 3\n")
        self.assertIn("ejecutor.py", protocolo.verificar_generador(self.base, self.d)[0])

    def test_archivo_nuevo_sin_commitear(self):
        (self.d / "nuevo.py").write_text("x = 3\n")
        self.assertIn("nuevo.py", protocolo.verificar_generador(self.base, self.d)[0])
        (self.d / "nuevo.py").unlink()
        (self.d / "registros").mkdir()
        (self.d / "registros" / "estado-c1.json").write_text("{}\n")  # lo que escribe una corrida: permitido
        self.assertEqual(protocolo.verificar_generador(self.base, self.d), [])

    def test_no_descendiente(self):
        git(self.d, "checkout", "-q", "--orphan", "otra")
        git(self.d, "commit", "-q", "-m", "otra historia")
        self.assertIn("no desciende", protocolo.verificar_generador(self.base, self.d)[0])

    def test_permitido(self):
        self.assertTrue(protocolo.permitido("datos/pruebas/corridas.csv"))
        self.assertTrue(protocolo.permitido("protocolo/protocolo-v1.json"))
        self.assertFalse(protocolo.permitido("datosx/a.csv"))
        self.assertFalse(protocolo.permitido("protocolo.py"))

if __name__ == "__main__":
    unittest.main()
