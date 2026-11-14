import collections
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import escenario  # noqa: E402

MIN, MAX = escenario.BRECHA


def muestra(versiones_objetivo=20000, nivel=5):
    """Escenarios con semillas 0, 1, 2, ... hasta juntar versiones_objetivo versiones."""
    escenarios = []
    for semilla in range(versiones_objetivo // nivel):
        escenarios.append(escenario.generar(semilla % 50 + 1, nivel, semilla))
    return escenarios


class Determinismo(unittest.TestCase):
    def test_misma_entrada_mismo_json(self):
        a = escenario.a_json(escenario.generar(1, 5, 101))
        b = escenario.a_json(escenario.generar(1, 5, 101))
        self.assertEqual(a.encode("utf-8"), b.encode("utf-8"))

    def test_otra_semilla_otro_json(self):
        a = escenario.a_json(escenario.generar(1, 5, 101))
        b = escenario.a_json(escenario.generar(1, 5, 102))
        self.assertNotEqual(a, b)

    def test_cli_byte_a_byte(self):
        with tempfile.TemporaryDirectory() as d:
            salidas = [Path(d) / "a" / "c3.json", Path(d) / "b" / "c3.json"]
            for salida in salidas:
                subprocess.run(
                    [sys.executable, str(RAIZ / "escenario.py"), "--corrida", "3", "--nivel", "3",
                     "--semilla", "7", "--salida", str(salida)],
                    check=True, capture_output=True,
                )
            self.assertEqual(salidas[0].read_bytes(), salidas[1].read_bytes())
            ver = subprocess.run(
                [sys.executable, str(RAIZ / "escenario.py"), "--ver", str(salidas[0])],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("release/c3-1", ver.stdout)


class Proporciones(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.versiones = [v for e in muestra() for v in e["versiones"]]

    def assertProporciones(self, conteo, esperadas, minimo=20000, tolerancia=0.01):
        total = sum(conteo.values())
        self.assertGreaterEqual(total, minimo)
        for valor, p in esperadas.items():
            observada = conteo.get(valor, 0) / total
            with self.subTest(valor=valor, esperada=p, observada=round(observada, 4)):
                self.assertLessEqual(abs(observada - p), tolerancia)
        self.assertEqual(set(conteo) - set(esperadas), set())

    def test_tipos(self):
        self.assertProporciones(collections.Counter(v["tipo"] for v in self.versiones), escenario.TIPOS)

    def test_actualizaciones(self):
        conteo = collections.Counter(
            sum(1 for e in v["eventos"] if e["accion"] == "actualizacion") for v in self.versiones
        )
        self.assertProporciones(conteo, escenario.ACTUALIZACIONES)

    def test_resultados(self):
        self.assertProporciones(collections.Counter(v["resultado"] for v in self.versiones), escenario.RESULTADOS)

    def test_primer_repo_de_combinacion(self):
        primeros = collections.Counter(
            v["eventos"][0]["repo"] for v in self.versiones if v["tipo"] == "combinacion"
        )
        # Solo ~17 % de las versiones son combinaciones (~3.400): ±1 punto sería ~1,2 desvíos estándar,
        # así que acá la tolerancia es ±3 puntos (~3,5 desvíos).
        self.assertProporciones(primeros, {"backend": 0.5, "frontend": 0.5}, minimo=3000, tolerancia=0.03)


class Reglas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.escenarios = muestra(versiones_objetivo=5000) + [
            escenario.generar(c, nivel, s) for c in (1, 2, 9) for nivel in escenario.NIVELES for s in range(40)
        ]

    def test_reglas(self):
        for esc in self.escenarios:
            c = esc["corrida"]
            with self.subTest(corrida=c, nivel=esc["nivel"], semilla=esc["semilla"]):
                self.revisar(esc)

    def revisar(self, esc):
        c = esc["corrida"]
        versiones = esc["versiones"]
        self.assertEqual([v["n"] for v in versiones], list(range(1, esc["nivel"] + 1)))
        self.assertEqual(esc["generador"]["version_formato"], 1)
        self.assertRegex(esc["generador"]["commit"], r"^([0-9a-f]{40}|sin-commit)$")

        aperturas_version = []
        for v in versiones:
            n = v["n"]
            self.assertEqual(v["rama"], f"release/c{c}-{n}")
            self.assertRegex(v["rama"], r"^release/c[0-9]+-[0-9]+$")
            self.assertEqual(v["repos"], escenario.REPOS_POR_TIPO[v["tipo"]])
            if v["tipo"] in escenario.TIPOS_CON_MIGRACION:
                self.assertEqual(v["migracion"], f"c{c}_{n}")
                self.assertRegex(v["migracion"], r"^c[0-9]+_[0-9]+$")
            else:
                self.assertIsNone(v["migracion"])
            self.assertIn(v["resultado"], escenario.RESULTADOS)

            eventos = v["eventos"]
            ts = [e["t"] for e in eventos]
            self.assertEqual(ts, sorted(ts), "eventos de la versión desordenados")
            self.assertTrue(all(isinstance(t, int) for t in ts))

            aperturas = [e for e in eventos if e["accion"] == "apertura"]
            self.assertEqual(sorted(e["repo"] for e in aperturas), sorted(v["repos"]))
            self.assertTrue(all(e["actualizacion"] == 0 for e in aperturas))
            self.assertEqual(eventos[0]["accion"], "apertura")
            inicio = eventos[0]["t"]
            aperturas_version.append(inicio)
            abre = {e["repo"]: e["t"] for e in aperturas}
            if v["tipo"] == "combinacion":
                primera, segunda = aperturas
                self.assertLess(eventos.index(primera), eventos.index(segunda))
                self.assertGreaterEqual(segunda["t"], primera["t"])
                self.assertTrue(MIN <= segunda["t"] - primera["t"] <= MAX)

            actualizaciones = [e for e in eventos if e["accion"] == "actualizacion"]
            self.assertLessEqual(len(actualizaciones), max(escenario.ACTUALIZACIONES))
            anterior = inicio
            k = collections.Counter()
            for e in actualizaciones:
                self.assertTrue(MIN <= e["t"] - anterior <= MAX, "brecha de actualización fuera de rango")
                self.assertLessEqual(abre[e["repo"]], e["t"], "actualización de un repo todavía no abierto")
                k[e["repo"]] += 1
                self.assertEqual(e["actualizacion"], k[e["repo"]])
                anterior = e["t"]

        self.assertEqual(aperturas_version[0], 0)
        for a, b in zip(aperturas_version, aperturas_version[1:]):
            self.assertTrue(MIN <= b - a <= MAX, "brecha de apertura fuera de rango")

        self.assertEqual(esc["catalogo_migraciones"], [v["migracion"] for v in versiones if v["migracion"]])
        self.assertEqual(len(set(esc["catalogo_migraciones"])), len(esc["catalogo_migraciones"]))

        planos = [{"n": v["n"], **e} for v in versiones for e in v["eventos"]]
        self.assertEqual(esc["eventos_ordenados"], sorted(planos, key=lambda e: (e["t"], e["n"])))

    def test_json_serializable_y_estable(self):
        esc = escenario.generar(1, 3, 101)
        self.assertEqual(json.loads(escenario.a_json(esc)), esc)
        self.assertEqual(
            list(esc),
            ["corrida", "nivel", "semilla", "generador", "parametros", "catalogo_migraciones", "versiones",
             "eventos_ordenados"],
        )


class Errores(unittest.TestCase):
    def test_nivel_invalido(self):
        for nivel in (0, 2, 4, 6, -1):
            with self.subTest(nivel=nivel), self.assertRaises(ValueError):
                escenario.generar(1, nivel, 1)

    def test_nivel_invalido_cli(self):
        r = subprocess.run(
            [sys.executable, str(RAIZ / "escenario.py"), "--corrida", "1", "--nivel", "2", "--semilla", "1",
             "--salida", "/tmp/no-se-escribe.json"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(Path("/tmp/no-se-escribe.json").exists())


if __name__ == "__main__":
    unittest.main()
