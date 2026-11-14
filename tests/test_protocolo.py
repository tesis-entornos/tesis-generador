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
        "terraform": {"fijado": TF},
        "generador": {"commit": "c" * 40},
        "escenario": protocolo.parametros_escenario(),
    }
    m["huella"] = protocolo.huella(m)
    return m


def vivo(m):
    return {"etags": dict(m["buildspecs"]["etags"]),
            "suite": {"imagenes": {"desplegar-efimero.yml": SUITE, "desplegar-fijo.yml": SUITE}, "playwright_retries": 2,
                      "simular_trabajo_seg": "0"},
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
        v["suite"]["simular_trabajo_seg"] = "120"
        v["pisadas"] = [{"proyecto": "tesis-efimero-desplegar-backend", "variable": "SIMULAR_TRABAJO_SEG", "valor": "60"}]
        self.assertEqual(len(protocolo.comparar(m, v)), 3)

    def test_tag_json_terraform_y_escenario(self):
        m = manifiesto()
        v = vivo(m)
        v["tag_json_main"]["tesis-frontend-cd"] = {"digest": "sha256:" + "0" * 64}
        v["terraform"]["tesis-efimero-cerrar-frontend"] = "cec2c7f3835bb569d0a90d49f8cdddccf5acd8c3"
        v["escenario_archivo"] = dict(v["escenario_archivo"], resultado={"integrada": 0.5, "declinada": 0.5})
        self.assertEqual(len(protocolo.comparar(m, v)), 3)


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
        pub = {"desplegar-efimero.yml": f"env:\n  variables:\n    SIMULAR_TRABAJO_SEG: \"0\"\n    SUITE_IMAGEN: {SUITE}\n",
               "desplegar-fijo.yml": f"    SUITE_IMAGEN: {SUITE}\n",
               "scripts/ejecutar-suite.sh": "docker run -e PLAYWRIGHT_RETRIES=2 \\\n"}
        s = protocolo.suite_de(pub)
        self.assertEqual(s, {"imagenes": {"desplegar-efimero.yml": SUITE, "desplegar-fijo.yml": SUITE},
                             "playwright_retries": 2, "simular_trabajo_seg": "0"})


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class Generador(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        git(self.d, "init", "-q", "-b", "main")
        for f in ("escenario.py", "acciones.py", "ejecutor.py"):
            (self.d / f).write_text("x = 1\n")
        git(self.d, "add", ".")
        git(self.d, "commit", "-q", "-m", "base")
        self.base = git(self.d, "rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def test_mismo_commit_y_descendiente_que_no_toca_protegidos(self):
        self.assertEqual(protocolo.verificar_generador(self.base, self.d), [])
        (self.d / "ejecutor.py").write_text("x = 2\n")
        git(self.d, "commit", "-q", "-am", "ejecutor")
        self.assertEqual(protocolo.verificar_generador(self.base, self.d), [])

    def test_descendiente_que_toca_escenario(self):
        (self.d / "escenario.py").write_text("x = 2\n")
        git(self.d, "commit", "-q", "-am", "cambia el escenario")
        d = protocolo.verificar_generador(self.base, self.d)
        self.assertEqual(len(d), 1)
        self.assertIn("escenario.py", d[0])

    def test_cambio_local_sin_commitear(self):
        (self.d / "acciones.py").write_text("x = 3\n")
        self.assertIn("acciones.py", protocolo.verificar_generador(self.base, self.d)[0])

    def test_no_descendiente(self):
        git(self.d, "checkout", "-q", "--orphan", "otra")
        git(self.d, "commit", "-q", "-m", "otra historia")
        self.assertIn("no desciende", protocolo.verificar_generador(self.base, self.d)[0])


if __name__ == "__main__":
    unittest.main()
