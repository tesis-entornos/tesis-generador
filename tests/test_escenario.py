import collections
import json
import random
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


class ActualizacionesDefinitivo(unittest.TestCase):
    """Experimento definitivo: P(0) a la mitad de la calibración; resto en la proporción medida (28 : 8 : 7)."""

    def test_pesos_suman_1_y_dan_los_valores(self):
        a = escenario.ACTUALIZACIONES
        self.assertEqual(list(a), [0, 1, 2, 3])
        self.assertAlmostEqual(sum(a.values()), 1.0, places=12)
        esperados = {0: 0.285, 1: 0.715 * 28 / 43, 2: 0.715 * 8 / 43, 3: 0.715 * 7 / 43}
        for k, p in esperados.items():
            with self.subTest(k=k):
                self.assertAlmostEqual(a[k], p, places=12)
        self.assertAlmostEqual(a[1], 0.465581, places=6)
        self.assertAlmostEqual(a[2], 0.133023, places=6)
        self.assertAlmostEqual(a[3], 0.116395, places=6)
        self.assertAlmostEqual(a[0], escenario.ACTUALIZACIONES_CALIBRACION[0] / 2, places=12)
        self.assertAlmostEqual(a[1] / a[2], 28 / 8, places=9)
        self.assertAlmostEqual(a[2] / a[3], 8 / 7, places=9)

    def test_regla_registrada_en_parametros(self):
        p = escenario.PARAMETROS
        self.assertEqual(p["actualizaciones_regla"], "P(0) a la mitad de la calibración; resto en la proporción medida")
        self.assertEqual(p["actualizaciones_calibracion"], {"0": 0.57, "1": 0.28, "2": 0.08, "3": 0.07})
        self.assertEqual(p["actualizaciones"], {str(k): v for k, v in escenario.ACTUALIZACIONES.items()})

    def test_100000_sorteos_a_1_punto(self):
        rng = random.Random(20261117)
        conteo = collections.Counter(escenario._sortear(rng, escenario.ACTUALIZACIONES) for _ in range(100000))
        self.assertEqual(set(conteo), {0, 1, 2, 3})
        for k, p in escenario.ACTUALIZACIONES.items():
            with self.subTest(k=k, esperada=p, observada=conteo[k] / 100000):
                self.assertLessEqual(abs(conteo[k] / 100000 - p), 0.01)

    def test_siguen_existiendo_versiones_sin_actualizaciones(self):
        versiones = [v for e in muestra(versiones_objetivo=2000) for v in e["versiones"]]
        sin = [v for v in versiones if not any(e["accion"] == "actualizacion" for e in v["eventos"])]
        self.assertGreater(len(sin), 0)
        self.assertLessEqual(abs(len(sin) / len(versiones) - 0.285), 0.03)


class ActualizacionesFijas(unittest.TestCase):
    """Diseño del tutor (17/11): con actualizaciones_fijas = k, toda versión tiene exactamente k actualizaciones; el
    resto del escenario se sortea con las mismas reglas. Sin el parámetro, nada cambia."""

    @classmethod
    def setUpClass(cls):
        cls.por_k = {
            k: [escenario.generar(c, nivel, s, actualizaciones_fijas=k)
                for c in (1, 9, 89) for nivel in escenario.NIVELES for s in range(60)]
            for k in escenario.ACTUALIZACIONES_FIJAS
        }

    @staticmethod
    def actualizaciones(v):
        return [e for e in v["eventos"] if e["accion"] == "actualizacion"]

    def test_toda_version_tiene_exactamente_k(self):
        for k, escenarios in self.por_k.items():
            for esc in escenarios:
                for v in esc["versiones"]:
                    with self.subTest(k=k, corrida=esc["corrida"], nivel=esc["nivel"], semilla=esc["semilla"], n=v["n"]):
                        self.assertEqual(len(self.actualizaciones(v)), k)
                self.assertEqual(sum(e["accion"] == "actualizacion" for e in esc["eventos_ordenados"]), k * esc["nivel"])

    def test_k_0_sin_eventos_de_actualizacion(self):
        for esc in self.por_k[0]:
            with self.subTest(corrida=esc["corrida"], nivel=esc["nivel"], semilla=esc["semilla"]):
                self.assertEqual([e for e in esc["eventos_ordenados"] if e["accion"] != "apertura"], [])
                for v in esc["versiones"]:
                    self.assertEqual(len(v["eventos"]), len(v["repos"]))
                    self.assertTrue(all(e["actualizacion"] == 0 for e in v["eventos"]))

    def test_resto_respeta_las_mismas_reglas(self):
        reglas = Reglas()
        for k, escenarios in self.por_k.items():
            for esc in escenarios:
                with self.subTest(k=k, corrida=esc["corrida"], nivel=esc["nivel"], semilla=esc["semilla"]):
                    reglas.revisar(esc)
                    self.assertEqual(esc["actualizaciones_fijas"], k)
                    self.assertEqual(esc["parametros"], escenario.PARAMETROS)

    def test_aperturas_iguales_a_las_del_sorteo_libre(self):
        # Los instantes de apertura se sortean antes que todo: el k fijo no los cambia.
        for k in escenario.ACTUALIZACIONES_FIJAS:
            for s in range(30):
                libre, fijo = escenario.generar(5, 5, s), escenario.generar(5, 5, s, actualizaciones_fijas=k)
                with self.subTest(k=k, semilla=s):
                    self.assertEqual([v["eventos"][0]["t"] for v in libre["versiones"]],
                                     [v["eventos"][0]["t"] for v in fijo["versiones"]])
                    self.assertEqual(libre["versiones"][0]["tipo"], fijo["versiones"][0]["tipo"])

    def test_composicion_y_resultados_en_proporcion(self):
        for k in escenario.ACTUALIZACIONES_FIJAS:
            versiones = [v for s in range(4000) for v in escenario.generar(1, 5, s, actualizaciones_fijas=k)["versiones"]]
            prop = Proporciones()
            with self.subTest(k=k):
                prop.assertProporciones(collections.Counter(v["tipo"] for v in versiones), escenario.TIPOS)
                prop.assertProporciones(collections.Counter(v["resultado"] for v in versiones), escenario.RESULTADOS)

    def test_repo_de_cada_actualizacion_entre_los_abiertos(self):
        # Combinaciones con k = 2: las dos actualizaciones caen en repos ya abiertos y, en la muestra, aparecen
        # actualizaciones de los dos repos (el repo se sortea entre los abiertos).
        repos = collections.Counter()
        for s in range(3000):
            for v in escenario.generar(1, 5, s, actualizaciones_fijas=2)["versiones"]:
                if v["tipo"] != "combinacion":
                    continue
                abre = {e["repo"]: e["t"] for e in v["eventos"] if e["accion"] == "apertura"}
                for e in self.actualizaciones(v):
                    self.assertLessEqual(abre[e["repo"]], e["t"])
                    repos[e["repo"]] += 1
        self.assertGreater(repos["backend"], 0)
        self.assertGreater(repos["frontend"], 0)

    def test_registrado_en_parametros(self):
        self.assertEqual(escenario.ACTUALIZACIONES_FIJAS, (0, 1, 2))
        self.assertEqual(escenario.PARAMETROS["actualizaciones_fijas"],
                         {"valores": [0, 1, 2], "regla": escenario.ACTUALIZACIONES_FIJAS_REGLA})

    def test_valores_invalidos(self):
        for k in (3, -1, True, False, "1", 1.0):
            with self.subTest(k=k), self.assertRaises(ValueError):
                escenario.generar(1, 3, 1, actualizaciones_fijas=k)
        r = subprocess.run(
            [sys.executable, str(RAIZ / "escenario.py"), "--corrida", "1", "--nivel", "3", "--semilla", "1",
             "--actualizaciones-fijas", "3", "--salida", "/tmp/no-se-escribe-k.json"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(Path("/tmp/no-se-escribe-k.json").exists())

    def test_cli_byte_a_byte_y_clave(self):
        with tempfile.TemporaryDirectory() as d:
            salidas = [Path(d) / "a" / "c89.json", Path(d) / "b" / "c89.json"]
            for salida in salidas:
                subprocess.run(
                    [sys.executable, str(RAIZ / "escenario.py"), "--corrida", "89", "--nivel", "3", "--semilla", "7",
                     "--actualizaciones-fijas", "2", "--salida", str(salida)],
                    check=True, capture_output=True,
                )
            self.assertEqual(salidas[0].read_bytes(), salidas[1].read_bytes())
            e = json.loads(salidas[0].read_text(encoding="utf-8"))
            self.assertEqual(e, escenario.generar(89, 3, 7, actualizaciones_fijas=2))
            self.assertEqual(list(e)[:5], ["corrida", "nivel", "semilla", "actualizaciones_fijas", "generador"])
            ver = subprocess.run([sys.executable, str(RAIZ / "escenario.py"), "--ver", str(salidas[0])],
                                 check=True, capture_output=True, text=True)
            self.assertIn("actualizaciones fijas 2", ver.stdout)


class SinParametroIgualQueAntes(unittest.TestCase):
    """Sin actualizaciones_fijas el generador es el de v4: los escenarios c61–c88 commiteados se regeneran con los
    mismos sorteos (todo igual salvo el commit del generador y la entrada nueva de PARAMETROS)."""

    def test_c61_a_c88_se_regeneran_igual(self):
        rutas = [RAIZ / "escenarios" / f"c{c}.json" for c in range(61, 89)]
        self.assertEqual(len(rutas), 28)
        for ruta in rutas:
            viejo = json.loads(ruta.read_text(encoding="utf-8"))
            nuevo = escenario.generar(viejo["corrida"], viejo["nivel"], viejo["semilla"])
            with self.subTest(escenario=ruta.name):
                self.assertNotIn("actualizaciones_fijas", nuevo)
                self.assertEqual(list(nuevo), list(viejo))
                for clave in ("corrida", "nivel", "semilla", "catalogo_migraciones", "versiones", "eventos_ordenados"):
                    self.assertEqual(nuevo[clave], viejo[clave], clave)
                self.assertEqual(nuevo["generador"]["version_formato"], viejo["generador"]["version_formato"])
                sin_nueva = {k: v for k, v in nuevo["parametros"].items() if k != "actualizaciones_fijas"}
                self.assertEqual(sin_nueva, viejo["parametros"])

    def test_none_es_el_default(self):
        self.assertEqual(escenario.generar(3, 5, 11), escenario.generar(3, 5, 11, actualizaciones_fijas=None))


if __name__ == "__main__":
    unittest.main()
