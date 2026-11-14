import json
import py_compile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import acciones  # noqa: E402


def escenario_de_prueba():
    """c7 con tres versiones: migracion (backend), solo_backend y combinacion."""
    return {
        "corrida": 7,
        "catalogo_migraciones": ["c7_1", "c7_3"],
        "versiones": [
            {"n": 1, "rama": "release/c7-1", "tipo": "migracion", "repos": ["backend"], "migracion": "c7_1",
             "eventos": [{"t": 0, "repo": "backend", "accion": "apertura", "actualizacion": 0},
                         {"t": 10, "repo": "backend", "accion": "actualizacion", "actualizacion": 1},
                         {"t": 20, "repo": "backend", "accion": "actualizacion", "actualizacion": 2}]},
            {"n": 2, "rama": "release/c7-2", "tipo": "solo_backend", "repos": ["backend"], "migracion": None,
             "eventos": [{"t": 5, "repo": "backend", "accion": "apertura", "actualizacion": 0}]},
            {"n": 3, "rama": "release/c7-3", "tipo": "combinacion", "repos": ["frontend", "backend"],
             "migracion": "c7_3",
             "eventos": [{"t": 7, "repo": "frontend", "accion": "apertura", "actualizacion": 0},
                         {"t": 9, "repo": "backend", "accion": "apertura", "actualizacion": 0},
                         {"t": 30, "repo": "frontend", "accion": "actualizacion", "actualizacion": 1}]},
        ],
    }


class Marcador(unittest.TestCase):
    def test_contenido_exacto(self):
        self.assertEqual(
            acciones.contenido_marcador(7, 3, 2),
            '{\n  "version": "c7-3",\n  "actualizacion": 2\n}\n',
        )

    def test_ruta_por_repo(self):
        e = escenario_de_prueba()
        self.assertIn("app/sintetico.json", acciones.archivos_apertura(e, 1, "backend"))
        self.assertIn("public/sintetico.json", acciones.archivos_apertura(e, 3, "frontend"))

    def test_apertura_con_k_cero(self):
        e = escenario_de_prueba()
        self.assertEqual(json.loads(acciones.archivos_apertura(e, 1, "backend")["app/sintetico.json"]),
                         {"version": "c7-1", "actualizacion": 0})


class Migraciones(unittest.TestCase):
    def setUp(self):
        self.e = escenario_de_prueba()

    def _compila(self, contenido):
        with tempfile.TemporaryDirectory() as d:
            archivo = Path(d) / "m.py"
            archivo.write_text(contenido, encoding="utf-8")
            py_compile.compile(str(archivo), doraise=True)

    def test_un_archivo_por_revision_del_catalogo(self):
        for n in (1, 2, 3):
            archivos = acciones.archivos_apertura(self.e, n, "backend")
            migraciones = sorted(r for r in archivos if r.startswith("app/alembic/versions/"))
            self.assertEqual(migraciones, ["app/alembic/versions/c7_1.py", "app/alembic/versions/c7_3.py"])

    def test_cabecera_de_todas(self):
        for n in (1, 2, 3):
            for revision in ("c7_1", "c7_3"):
                texto = acciones.archivos_apertura(self.e, n, "backend")[f"app/alembic/versions/{revision}.py"]
                self.assertIn(f"revision = '{revision}'\n", texto)
                self.assertIn("down_revision = 'e3acbde9bd2f'\n", texto)
                self.assertIn("branch_labels = None\n", texto)
                self.assertIn("depends_on = None\n", texto)
                self._compila(texto)

    def test_propia_crea_y_borra_la_tabla(self):
        texto = acciones.archivos_apertura(self.e, 3, "backend")["app/alembic/versions/c7_3.py"]
        self.assertIn("op.create_table(\n        'tesis_c7_3',", texto)
        self.assertIn("sa.Column('id', sa.Integer(), autoincrement=True, nullable=False)", texto)
        self.assertIn("sa.Column('nota', sa.Text(), nullable=True)", texto)
        self.assertIn("sa.PrimaryKeyConstraint('id')", texto)
        self.assertIn("def downgrade():\n    op.drop_table('tesis_c7_3')\n", texto)

    def test_ajenas_vacias(self):
        for n, revision in ((1, "c7_3"), (2, "c7_1"), (2, "c7_3"), (3, "c7_1")):
            texto = acciones.archivos_apertura(self.e, n, "backend")[f"app/alembic/versions/{revision}.py"]
            self.assertIn("def upgrade():\n    pass\n", texto)
            self.assertIn("def downgrade():\n    pass\n", texto)
            self.assertNotIn("create_table", texto)

    def test_estructura_ejecutable(self):
        """upgrade/downgrade llaman a op como se espera (op y sa simulados)."""
        llamadas = []

        class Op:
            def __getattr__(self, nombre):
                return lambda *a, **kw: llamadas.append((nombre, a[0] if a else None))

        class Sa:
            def __getattr__(self, nombre):
                return lambda *a, **kw: nombre

        texto = acciones.contenido_migracion("c7_3", True).replace("from alembic import op\n", "").replace(
            "import sqlalchemy as sa\n", "")
        ns = {"op": Op(), "sa": Sa()}
        exec(compile(texto, "c7_3.py", "exec"), ns)
        ns["upgrade"]()
        ns["downgrade"]()
        self.assertEqual(llamadas, [("create_table", "tesis_c7_3"), ("drop_table", "tesis_c7_3")])


class Objetivo(unittest.TestCase):
    def test_con_migracion_propia(self):
        e = escenario_de_prueba()
        self.assertEqual(acciones.archivos_apertura(e, 1, "backend")["app/alembic/objetivo"], "c7_1\n")
        self.assertEqual(acciones.archivos_apertura(e, 3, "backend")["app/alembic/objetivo"], "c7_3\n")

    def test_sin_migracion_propia(self):
        e = escenario_de_prueba()
        self.assertEqual(acciones.archivos_apertura(e, 2, "backend")["app/alembic/objetivo"], "e3acbde9bd2f\n")

    def test_catalogo_vacio(self):
        e = escenario_de_prueba()
        e["catalogo_migraciones"] = []
        e["versiones"][0]["migracion"] = None
        archivos = acciones.archivos_apertura(e, 1, "backend")
        self.assertEqual(set(archivos), {"app/sintetico.json", "app/alembic/objetivo"})
        self.assertEqual(archivos["app/alembic/objetivo"], "e3acbde9bd2f\n")


class Frontend(unittest.TestCase):
    def test_solo_marcador(self):
        e = escenario_de_prueba()
        self.assertEqual(list(acciones.archivos_apertura(e, 3, "frontend")), ["public/sintetico.json"])


class Validacion(unittest.TestCase):
    def setUp(self):
        self.e = escenario_de_prueba()

    def test_repo_que_la_version_no_toca(self):
        with self.assertRaises(acciones.ErrorAccion):
            acciones.validar_apertura(self.e, 1, "frontend")

    def test_k_debe_ser_el_siguiente(self):
        acciones.validar_actualizacion(self.e, 1, "backend", 1, 0)
        acciones.validar_actualizacion(self.e, 1, "backend", 2, 1)
        with self.assertRaises(acciones.ErrorAccion):
            acciones.validar_actualizacion(self.e, 1, "backend", 2, 0)

    def test_k_debe_estar_en_el_escenario(self):
        with self.assertRaises(acciones.ErrorAccion):
            acciones.validar_actualizacion(self.e, 2, "backend", 1, 0)
        with self.assertRaises(acciones.ErrorAccion):
            acciones.validar_actualizacion(self.e, 3, "backend", 1, 0)  # la actualización es de frontend


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class EnSecoConGit(unittest.TestCase):
    """abrir/actualizar --dry-run contra un remoto local de prueba."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        origen = d / "origen"
        (origen / "app" / "alembic" / "versions").mkdir(parents=True)
        (origen / "public").mkdir()
        (origen / "app" / "alembic" / "versions" / "e3acbde9bd2f_x.py").write_text("x = 1\n")
        (origen / "public" / "index.html").write_text("<p>\n")
        _git(origen, "init", "--quiet", "-b", "main")
        _git(origen, "add", ".")
        _git(origen, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "base")
        self.base = _git(origen, "rev-parse", "HEAD")
        self.kw = dict(trabajo=d / "trabajo", registros=d / "registros", remoto=str(origen), base=self.base)
        self.e = escenario_de_prueba()

    def tearDown(self):
        self.tmp.cleanup()

    def test_apertura_y_actualizaciones_backend(self):
        registro, diff = acciones.abrir(self.e, 1, "backend", True, **self.kw)
        clon = self.kw["trabajo"] / "backend"
        self.assertEqual(registro["k"], 0)
        self.assertTrue(registro["dry_run"])
        self.assertIsNone(registro["pr"])
        self.assertEqual(_git(clon, "log", "-1", "--format=%s|%an|%ae"),
                         "c7-1 backend apertura|martincho|mcometta211@alumnos.iua.edu.ar")
        self.assertEqual(
            _git(clon, "diff", "--name-only", f"{self.base}..HEAD").split("\n"),
            ["app/alembic/objetivo", "app/alembic/versions/c7_1.py", "app/alembic/versions/c7_3.py",
             "app/sintetico.json"],
        )
        apertura = _git(clon, "rev-parse", "HEAD")

        for k in (1, 2):
            registro, _ = acciones.actualizar(self.e, 1, "backend", k, True, **self.kw)
            self.assertEqual(_git(clon, "log", "-1", "--format=%s"), f"c7-1 backend actualizacion {k}")
            self.assertEqual(_git(clon, "diff", "--name-only", "HEAD~1..HEAD"), "app/sintetico.json")
        # Las actualizaciones no tocaron catálogo ni objetivo.
        self.assertEqual(_git(clon, "diff", "--name-only", f"{apertura}..HEAD"), "app/sintetico.json")
        self.assertEqual(json.loads((clon / "app/sintetico.json").read_text()), {"version": "c7-1", "actualizacion": 2})

        # Nada llegó al remoto y el registro fue al archivo de dry-run.
        self.assertEqual(_git(clon, "ls-remote", "--heads", "origin", "release/c7-1"), "")
        lineas = (self.kw["registros"] / "dryrun-c7.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(x)["k"] for x in lineas], [0, 1, 2])
        self.assertFalse((self.kw["registros"] / "c7.jsonl").exists())

    def test_actualizacion_fuera_de_orden(self):
        acciones.abrir(self.e, 1, "backend", True, **self.kw)
        with self.assertRaises(acciones.ErrorAccion):
            acciones.actualizar(self.e, 1, "backend", 2, True, **self.kw)

    def test_frontend_solo_marcador(self):
        acciones.abrir(self.e, 3, "frontend", True, **self.kw)
        clon = self.kw["trabajo"] / "frontend"
        self.assertEqual(_git(clon, "diff", "--name-only", f"{self.base}..HEAD"), "public/sintetico.json")
        acciones.actualizar(self.e, 3, "frontend", 1, True, **self.kw)
        self.assertEqual(_git(clon, "diff", "--name-only", f"{self.base}..HEAD"), "public/sintetico.json")


if __name__ == "__main__":
    unittest.main()
