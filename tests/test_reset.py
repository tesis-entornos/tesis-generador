import copy
import json
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import reset  # noqa: E402

BASE = "582621604637.dkr.ecr.us-east-2.amazonaws.com/tesis-backend@sha256:" + "e" * 64
OTRA = "582621604637.dkr.ecr.us-east-2.amazonaws.com/tesis-backend@sha256:" + "0" * 64


def task_definition(imagen=OTRA):
    """Como la devuelve describe-task-definition (con campos no registrables)."""
    return {
        "taskDefinitionArn": "arn:aws:ecs:us-east-2:491265021424:task-definition/fijo-compartido-backend:4",
        "family": "fijo-compartido-backend",
        "taskRoleArn": "arn:aws:iam::491265021424:role/tarea",
        "executionRoleArn": "arn:aws:iam::491265021424:role/ejecucion",
        "networkMode": "awsvpc",
        "cpu": "512",
        "memory": "1024",
        "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        "requiresCompatibilities": ["FARGATE"],
        "volumes": [],
        "containerDefinitions": [
            {"name": "backend", "image": imagen, "essential": True,
             "environment": [{"name": "PROJECT_NAME", "value": "x"}],
             "secrets": [{"name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:...:db"}],
             "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/ecs/fijo"}}},
            {"name": "lateral", "image": "otra/imagen:1", "essential": False},
        ],
        "revision": 4,
        "status": "ACTIVE",
        "requiresAttributes": [{"name": "com.amazonaws.ecs.capability.logging-driver.awslogs"}],
        "placementConstraints": [],
        "compatibilities": ["EC2", "FARGATE"],
        "registeredAt": "2026-11-14T12:30:00-03:00",
        "registeredBy": "arn:aws:sts::491265021424:assumed-role/x",
    }


class CopiaTaskDefinition(unittest.TestCase):
    def test_solo_campos_registrables(self):
        copia = reset.copia_task_definition(task_definition(), BASE)
        self.assertEqual(set(copia), set(reset.CAMPOS_TD))
        for campo in ("taskDefinitionArn", "revision", "status", "requiresAttributes", "compatibilities",
                      "registeredAt", "registeredBy"):
            self.assertNotIn(campo, copia)

    def test_cambia_solo_la_imagen_del_backend(self):
        td = task_definition()
        copia = reset.copia_task_definition(td, BASE)
        self.assertEqual(copia["containerDefinitions"][0]["image"], BASE)
        esperado = copy.deepcopy(td["containerDefinitions"])
        esperado[0]["image"] = BASE
        self.assertEqual(copia["containerDefinitions"], esperado)
        for campo in reset.CAMPOS_TD:
            if campo != "containerDefinitions":
                self.assertEqual(copia[campo], td[campo])

    def test_no_modifica_el_original(self):
        td = task_definition()
        antes = json.dumps(td, sort_keys=True)
        reset.copia_task_definition(td, BASE)
        self.assertEqual(json.dumps(td, sort_keys=True), antes)

    def test_campos_ausentes_no_se_inventan(self):
        td = task_definition()
        del td["taskRoleArn"], td["runtimePlatform"]
        copia = reset.copia_task_definition(td, BASE)
        self.assertNotIn("taskRoleArn", copia)
        self.assertNotIn("runtimePlatform", copia)

    def test_sin_contenedor_backend(self):
        td = task_definition()
        td["containerDefinitions"] = td["containerDefinitions"][1:]
        with self.assertRaises(reset.ErrorReset):
            reset.copia_task_definition(td, BASE)
        with self.assertRaises(reset.ErrorReset):
            reset.imagen_contenedor(td)

    def test_imagen_contenedor(self):
        self.assertEqual(reset.imagen_contenedor(task_definition(BASE)), BASE)

    def test_override(self):
        o = reset.overrides_reset()
        self.assertEqual(o, {"containerOverrides": [{
            "name": "backend", "command": ["python", "scripts/reset_baseline.py"],
            "environment": [{"name": "RESET_CONFIRM", "value": "app_compartido"}]}]})


def datos_ok():
    return {
        "builds": {p: ["SUCCEEDED", "FAILED"] for ps in reset.PROYECTOS.values() for p in ps},
        "servicios_efimero": [],
        "disponibles": [],
        "prs": {r: ["dependabot/pip/x", "feature/y"] for r in reset.REPOS_PR},
    }


class Precondiciones(unittest.TestCase):
    def test_todo_ok(self):
        self.assertEqual(reset.validar_precondiciones(datos_ok()), [])
        self.assertEqual(len(datos_ok()["builds"]), 8)

    def test_build_en_curso(self):
        d = datos_ok()
        d["builds"]["tesis-fijo-desplegar-frontend"] = ["IN_PROGRESS", "SUCCEEDED"]
        fallos = reset.validar_precondiciones(d)
        self.assertEqual(len(fallos), 1)
        self.assertIn("tesis-fijo-desplegar-frontend", fallos[0])

    def test_servicio_efimero_activo(self):
        d = datos_ok()
        d["servicios_efimero"] = ["efimero-release-c1-1-backend"]
        self.assertEqual(len(reset.validar_precondiciones(d)), 1)

    def test_disponible(self):
        d = datos_ok()
        d["disponibles"] = ["entornos/release-c1-1/disponible.json"]
        self.assertIn("disponible.json", reset.validar_precondiciones(d)[0])

    def test_pr_release_abierto(self):
        d = datos_ok()
        d["prs"]["tesis-backend-cd"] = ["release/c1-2", "hotfix/c1-1"]
        fallos = reset.validar_precondiciones(d)
        self.assertEqual(len(fallos), 1)
        self.assertIn("release/c1-2", fallos[0])
        self.assertNotIn("hotfix", fallos[0])

    def test_varios_fallos(self):
        d = datos_ok()
        d["builds"]["tesis-build-backend"] = ["IN_PROGRESS"]
        d["disponibles"] = ["entornos/x/disponible.json"]
        d["prs"]["tesis-frontend"] = ["release/c2-1"]
        self.assertEqual(len(reset.validar_precondiciones(d)), 3)


class Logs(unittest.TestCase):
    LINEAS = [
        "INFO:reset_baseline:Dropping database app_compartido",
        "INFO  [alembic.runtime.migration] Running upgrade  -> e2412789c190, Initialize models",
        "{",
        '  "baseline_ok": true,',
        '  "cache_deleted": 3,',
        '  "counts": {',
        '    "users": 1',
        "  },",
        '  "database": "app_compartido"',
        "}",
    ]

    def test_extraer_reporte(self):
        r = reset.extraer_reporte(self.LINEAS)
        self.assertEqual(r, {"baseline_ok": True, "cache_deleted": 3, "counts": {"users": 1}, "database": "app_compartido"})

    def test_sin_reporte(self):
        self.assertIsNone(reset.extraer_reporte(self.LINEAS[:2]))
        self.assertIsNone(reset.extraer_reporte(self.LINEAS[:-1]))

    def test_redactar(self):
        self.assertEqual(reset.redactar("postgresql+psycopg://app:s3cr3t@db.x:5432/app_compartido"),
                         "postgresql+psycopg://***:***@db.x:5432/app_compartido")
        self.assertEqual(reset.redactar("https://release-c1-1.efimero.x/ready"), "https://release-c1-1.efimero.x/ready")


class BuildsYRamas(unittest.TestCase):
    def test_elegir_build(self):
        builds = [
            {"id": "p:viejo", "resolvedSourceVersion": "abc"},
            {"id": "p:otro", "resolvedSourceVersion": "zzz", "sourceVersion": "zzz"},
            {"id": "p:nuevo", "sourceVersion": "abc"},
        ]
        self.assertEqual(reset.elegir_build(builds, "abc", {"p:viejo"})["id"], "p:nuevo")
        self.assertIsNone(reset.elegir_build(builds, "qqq", set()))

    def test_accion_rama(self):
        self.assertEqual(reset.accion_rama(None, "a"), "crear")
        self.assertEqual(reset.accion_rama("a", "a"), "nada")
        self.assertEqual(reset.accion_rama("b", "a"), "actualizar")

    def test_validar_validacion(self):
        self.assertEqual(reset.validar_validacion({"build_ok": True, "suite": {"resultado": "ok"}}), [])
        self.assertEqual(len(reset.validar_validacion({"build_ok": False, "suite": None})), 2)
        self.assertEqual(len(reset.validar_validacion({"build_ok": True, "suite": {"resultado": "fallo"}})), 1)


if __name__ == "__main__":
    unittest.main()
