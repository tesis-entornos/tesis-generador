import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import depurador  # noqa: E402

RESTOS = [
    {"tipo": "servicio", "nombre": "efimero-release-c9-2-backend", "rama": "release/c9-2"},
    {"tipo": "disponible", "clave": "entornos/release-c9-2/disponible.json", "rama": "release/c9-2"},
    {"tipo": "cloudfront", "arn": "arn:aws:cloudfront::1:distribution/X", "corrida": "c9", "rama": "release/c9-3"},
    {"tipo": "regla_alb", "prioridad": "10", "hosts": ["release-c9-2.efimero.x"]},
    {"tipo": "pr", "repo": "tesis-backend-cd", "numero": 4, "head": "release/c9-2", "base": "dev"},
    {"tipo": "rama", "repo": "tesis-backend-cd", "rama": "release/c9-2"},
    {"tipo": "rama", "repo": "tesis-backend", "rama": "release/c9-2"},
    {"tipo": "build", "id": "tesis-efimero-desplegar-backend:abc"},
]


class Simulada:
    """Plataforma de prueba: los restos desaparecen cuando se actúa sobre ellos."""

    def __init__(self, restos):
        self.restos = [dict(r) for r in restos]
        self.llamadas = []

    def buscar(self):
        return [dict(r) for r in self.restos]

    def esperar_builds(self, log):
        self.llamadas.append(("esperar_builds",))
        self.restos = [r for r in self.restos if r["tipo"] != "build"]

    def cierre_manual(self, rama, log):
        self.llamadas.append(("cierre_manual", rama))
        self.restos = [r for r in self.restos if not (r["tipo"] in ("servicio", "disponible", "cloudfront", "regla_alb")
                                                     and (r.get("rama") == rama or rama.replace("/", "-") in " ".join(r.get("hosts", []))))]
        return {"build_id": "b", "status": "SUCCEEDED", "resultado": "ok", "accion": "manual"}

    def cerrar_pr(self, repo, numero):
        self.llamadas.append(("cerrar_pr", repo, numero))
        self.restos = [r for r in self.restos if not (r["tipo"] == "pr" and r["numero"] == numero)]

    def borrar_rama(self, repo, rama):
        self.llamadas.append(("borrar_rama", repo, rama))
        self.restos = [r for r in self.restos if not (r["tipo"] == "rama" and r["repo"] == repo)]
        return True


class Puras(unittest.TestCase):
    def test_slugs(self):
        self.assertEqual(depurador.slug_de_servicio("efimero-release-c96-3-frontend"), "release-c96-3")
        self.assertEqual(depurador.rama_de_slug("release-c96-3"), "release/c96-3")
        self.assertEqual(depurador.rama_de_slug("hotfix-c2-1"), "hotfix/c2-1")
        self.assertIsNone(depurador.rama_de_slug("compartido"))
        self.assertIsNone(depurador.slug_de_servicio("fijo-compartido-backend"))

    def test_entornos_vivos_sin_repetir(self):
        self.assertEqual(depurador.entornos_vivos(RESTOS), ["release/c9-2", "release/c9-3"])

    def test_plan_ordenado(self):
        pasos = depurador.plan(RESTOS)
        self.assertEqual(pasos[0], ("esperar_builds",))
        nombres = [p[0] for p in pasos]
        # cierres antes de los PR, PR antes de las ramas, y espera después de cerrar PR y al final
        self.assertLess(nombres.index("cierre_manual"), nombres.index("cerrar_pr"))
        self.assertLess(nombres.index("cerrar_pr"), nombres.index("borrar_rama"))
        self.assertEqual(nombres[nombres.index("cerrar_pr") + 1], "esperar_builds")
        self.assertEqual(nombres[-1], "esperar_builds")
        self.assertEqual(nombres.count("cierre_manual"), 2)

    def test_plan_vacio(self):
        self.assertEqual(depurador.plan([]), [("esperar_builds",)])


class Depurar(unittest.TestCase):
    def test_sin_restos_no_actua(self):
        s = Simulada([])
        informe = depurador.depurar(s, log=lambda *_: None)
        self.assertEqual((informe["encontrados"], informe["acciones"], informe["restantes"]), ([], [], []))
        self.assertEqual(s.llamadas, [])

    def test_limpia_y_devuelve_lo_encontrado_antes_de_actuar(self):
        s = Simulada(RESTOS)
        informe = depurador.depurar(s, log=lambda *_: None)
        self.assertEqual(len(informe["encontrados"]), len(RESTOS))
        self.assertEqual(informe["restantes"], [])
        self.assertIn(("cierre_manual", "release/c9-3"), s.llamadas)
        self.assertEqual(s.llamadas[0], ("esperar_builds",))

    def test_solo_buscar(self):
        s = Simulada(RESTOS)
        informe = depurador.depurar(s, solo_buscar=True, log=lambda *_: None)
        self.assertEqual(s.llamadas, [])
        self.assertEqual(len(informe["restantes"]), len(RESTOS))


if __name__ == "__main__":
    unittest.main()
