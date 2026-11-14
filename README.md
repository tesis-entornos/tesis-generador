# tesis-generador

Generador determinista de los escenarios de carga del experimento de la tesis. Para cada corrida `c<N>`
genera las versiones sintéticas (ramas `release/c<N>-<n>`) y para cada una decide:

- el tipo y los repos que toca;
- si tiene migración propia;
- si se integra o se declina;
- la línea de tiempo de aperturas y actualizaciones.

En cada evento, el contador `actualizacion` es el valor `k` que va al marcador `sintetico.json` del repo.
Es 0 en la apertura y suma 1 en cada actualización de ese mismo repo.

Usa solo la biblioteca estándar de Python 3, sin dependencias. Toda la aleatoriedad sale de un único
`random.Random(semilla)`, con los sorteos siempre en el mismo orden. Los mismos `(corrida, nivel,
semilla)` dan el mismo JSON byte a byte. El archivo registra el commit del generador
(`generador.commit`) y la versión del formato.

## Uso

```sh
python3 escenario.py --corrida 1 --nivel 3 --semilla 101 --salida escenarios/c1.json
python3 escenario.py --ver escenarios/c1.json      # línea de tiempo legible
python3 -m unittest discover -s tests              # pruebas
```

Desde Python: `escenario.generar(corrida, nivel, semilla) -> dict` y `escenario.a_json(dict) -> str`.

## Parámetros

Todos los tiempos se miden en segundos desde el inicio de la corrida y son enteros. `U(0, 600)` es un
entero uniforme entre 0 y 600, con los dos extremos incluidos.

| Parámetro | Valor |
|---|---|
| Nivel (versiones por corrida) | 1, 3 o 5; versiones `n = 1..nivel` |
| Apertura de la versión 1 | t = 0 |
| Apertura de cada versión siguiente | U(0, 600) desde la apertura de la versión anterior |
| Tipo | `solo_frontend` 0,19 · `solo_backend` 0,23 · `migracion` 0,41 · `combinacion` 0,17 |
| Repos | `solo_frontend` → frontend · `solo_backend`, `migracion` → backend · `combinacion` → backend + frontend |
| Migración propia | `c<N>_<n>` en `migracion` y `combinacion`; `null` en el resto |
| Combinación | El primer repo (50/50) abre en la apertura de la versión; el segundo, U(0, 600) después |
| Actualizaciones por versión | 0 (0,57) · 1 (0,28) · 2 (0,08) · 3 (0,07) |
| Cada actualización | U(0, 600) desde el evento anterior de la cadena, en un repo sorteado entre los ya abiertos |
| Resultado | `integrada` 0,75 · `declinada` 0,25 |

**Cadena de actualizaciones.** La primera actualización se mide desde la apertura de la versión y cada
una de las siguientes desde la actualización anterior. En una combinación, la segunda apertura queda
fija respecto de la primera y no corta la cadena. El repo de cada actualización se sortea entre los repos
de la versión cuya apertura es anterior o igual a ese instante. Si dos eventos de una versión caen en el
mismo `t`, la apertura va primero. Con una brecha de 0, la segunda apertura de una combinación puede
coincidir en `t` con la primera, pero en la lista queda siempre después.

**Orden de los sorteos, por versión.** Primero la brecha de apertura (solo si n > 1). Después el tipo y,
si es combinación, el primer repo y la brecha del segundo. Luego la cantidad de actualizaciones y, para
cada una, su brecha y su repo. Al final, el resultado.

## Formato (`version_formato: 1`)

```json
{
  "corrida": 1, "nivel": 3, "semilla": 101,
  "generador": {"commit": "<sha del HEAD o sin-commit>", "version_formato": 1},
  "parametros": {"...": "todas las proporciones y rangos de la tabla"},
  "catalogo_migraciones": ["c1_1", "..."],
  "versiones": [
    {"n": 1, "rama": "release/c1-1", "tipo": "migracion", "repos": ["backend"], "migracion": "c1_1",
     "resultado": "integrada",
     "eventos": [{"t": 0, "repo": "backend", "accion": "apertura", "actualizacion": 0}]}
  ],
  "eventos_ordenados": [{"n": 1, "t": 0, "repo": "backend", "accion": "apertura", "actualizacion": 0}]
}
```

`eventos_ordenados` contiene los mismos eventos de todas las versiones, cada uno con su `n`, ordenados
por `t` y después por `n`.

Los escenarios del experimento van en `escenarios/c<N>.json`. Esa carpeta todavía está vacía.

## Acciones (`acciones.py`)

```sh
python3 acciones.py abrir      --escenario escenarios/c1.json --n 1 --repo backend [--dry-run]
python3 acciones.py actualizar --escenario escenarios/c1.json --n 1 --repo backend --k 1 [--dry-run]
```

- **abrir**: en el clon de trabajo `.trabajo/<repo>` (no versionado) hace `fetch`, `checkout -B release/c<N>-<n>`
  desde el commit de línea base (backend `afc16b1d85fc`, frontend `dd2a13341c32`), escribe el contenido
  sintético con `actualizacion = 0`, hace commit `c<N>-<n> <repo> apertura`, push y `gh pr create` contra `main`.
- **actualizar**: hace checkout de la rama existente, reescribe solo el marcador con `actualizacion = k`, hace
  commit `c<N>-<n> <repo> actualizacion <k>` y push.
- Validaciones previas: la versión toca ese repo; `k` es el siguiente del marcador de la rama y el escenario
  tiene esa actualización.

Contenido sintético:

- Marcador `app/sintetico.json` (backend) o `public/sintetico.json` (frontend): `{"version": "c<N>-<n>", "actualizacion": k}`.
- Solo backend, en toda rama de la corrida: `app/alembic/versions/<rev>.py` por cada revisión de
  `catalogo_migraciones`, todas con `down_revision = "e3acbde9bd2f"`. La propia crea/borra la tabla
  `tesis_<rev>` (id Integer PK, nota Text nullable); las demás tienen `pass`. `app/alembic/objetivo` tiene la
  revisión propia o `e3acbde9bd2f` si la versión no tiene migración.

Cada acción agrega una línea JSON a `registros/c<N>.jsonl` (`t_real`, `accion`, `n`, `repo`, `rama`, `k`,
`commit`, `pr`, `pr_created_at`, `dry_run`). Con `--dry-run` todo queda local (sin push ni PR), se imprime el
diff contra la línea base y el registro va a `registros/dryrun-c<N>.jsonl`, que no se versiona.

## Reset de inicio de corrida (`reset.py`)

```sh
python3 reset.py --corrida N [--dry-run]
```

Deja el entorno compartido del fijo en la línea base (el `tag.json` de main de cada -cd): precondiciones
(sin builds en curso, efímero vacío, sin PR release/* abiertos), reset de datos con `scripts/reset_baseline.py`
en una tarea one-off, despliegue de la línea base en dev de backend-cd y frontend-cd (esperando cada build
del fijo con build_ok y suite ok), segundo reset de datos, y `main-fijo`/`main-efimero` al SHA de main en los
dos -cd. El registro va a `registros/reset-c<N>.json`; su `fin` es el t = 0 de la corrida. `--dry-run` solo
consulta y muestra lo que haría.

## Depurador, expiración y cierre de corrida

- `python3 depurador.py [--solo-buscar]`: busca restos (servicios ACTIVE, disponible.json, reglas no default del ALB,
  distribuciones con tag corrida, ramas release/*, PR abiertos release/*, builds en curso) y los limpia (cierre manual
  de entornos vivos, cierre de PR, borrado de ramas, siempre sin builds en curso). El ejecutor lo llama al final
  (solo búsqueda si la corrida se abortó).
- `ejecutor.py --expiracion-seg S` (por defecto 4 días): una versión sin eventos propios durante más de S segundos y
  sin cerrar en algún modelo expira; espera a sus builds en curso, registra la validación final que haya llegado (con
  marca de expirada) y se cierra como declinada en los modelos abiertos.
- `python3 cierre_corrida.py --corrida N`: validez de la corrida (con el motivo de cada criterio que falle) y filas de
  la corrida en `datos/versiones.csv` y `datos/corridas.csv`; informe en `registros/cierre-c<N>.json`. Las versiones
  expiradas no entran en las estadísticas de corridas.csv.
