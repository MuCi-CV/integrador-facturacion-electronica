# Spec — Migración a Python 3.10 + Django 5.2 LTS

> Estado: aprobada la estrategia (enfoque A, cinco fases) el 2026-08-28.
> Todo el trabajo de código va en `feature/django-52-lts`, para poder volver a `main`.

## Problema

Producción corre **Django 3.2.25 sobre Python 3.7.17**. Las dos versiones están fuera de
soporte:

- **Django 3.2 LTS perdió soporte en abril de 2024.** Hace más de dos años que no recibe
  parches de seguridad.
- **Python 3.7 terminó su vida útil en junio de 2023.** El propio `cryptography` instalado
  ya avisa en cada arranque que va a dejar de soportarlo.

No es deuda de modernización, es deuda de seguridad.

**4.2 LTS también venció** (abril de 2026), así que no sirve como escalón con soporte:
**5.2 LTS es el único destino LTS vigente**.

### La restricción que ordena el trabajo

**Django 5.2 requiere Python ≥ 3.10.** Son dos saltos y el intérprete va primero.

## Objetivo

Producción corriendo **Python 3.10 + Django 5.2 LTS**, con el venv anterior intacto para
poder volver atrás en segundos, y con el entorno local igualado a 5.2 para que un verde
local vuelva a significar algo.

## No objetivos

- **Llegar a Django 6.** Ver "El muro de PyMySQL": exige cambiar de driver de base y
  agregar dependencias de compilación al sistema. Es otro proyecto.
- **Igualar el intérprete local** al de producción (3.12 vs 3.10). Decisión tomada: la
  brecha que costó errores era la del *framework*, y esa desaparece. El intérprete lo
  sigue cubriendo `verificar-en-stack-produccion.sh`.
- Los 67 parches de terceros pendientes, el `.env` legible por todos, y
  `runretryfaileds.sh` (roto y muerto). Todos anotados en otro lado.

## Lo que ya se verificó, y por qué cambia el plan

Todo lo de esta sección se midió el 2026-08-28 contra los entornos reales, no se asumió.

### 1. El intérprete ya está instalado

El servidor es **Ubuntu 22.04.5 LTS** y tiene **`/usr/bin/python3.10`** además del 3.7.
No hay que compilar ni agregar repositorios: el requisito duro de Django 5.2 ya está
satisfecho.

### 2. No hay ni una migración interna nueva entre 3.2.25 y 5.2.17

Comparando los archivos de migración de las apps internas en los dos entornos:

| app | 3.2.25 (producción) | 5.2.17 (destino) | última migración |
|---|---|---|---|
| `admin` | 3 | 3 | `0003_logentry_add_action_flag_choices` |
| `auth` | 12 | 12 | `0012_alter_user_first_name_max_length` |
| `contenttypes` | 2 | 2 | `0002_remove_content_type_name` |
| `sessions` | 1 | 1 | `0001_initial` |

Idénticas. Y `core` no tiene migraciones pendientes (la 0008 se aplicó el 2026-08-28).

**Consecuencia central para el riesgo:** el `migrate` posterior al upgrade es **no-op**, así
que **no queda estado de base adelantado** y el rollback por venv es *completo*, no parcial.
Esto es lo que convierte al backup de requisito en prudencia.

### 3. Los dos cambios de default que rompen en silencio ya están cubiertos

- **`USE_TZ`**: Django 5.0 cambió su default de `False` a `True`. `settings.py:174` ya lo
  fija en `True` explícitamente, así que el cambio no afecta. Importa porque el proyecto
  tiene una trampa de husos conocida (shell del servidor en UTC, Django en `America/Asuncion`).
- **`DEFAULT_AUTO_FIELD`**: `settings.py:181` ya lo fija en `BigAutoField`, y la 0001 ya usa
  `BigAutoField`. Sin warning W042 ni churn de migraciones.

### 4. `USE_L10N` es peso muerto, no un bloqueante

Removido en Django 5.0. Probado bajo 5.2.17: `manage.py check` pasa **sin error y sin
warning** — Django ignora los settings que no conoce. Se saca por limpieza, no por
necesidad.

### 5. El muro de PyMySQL, y por qué 5.2 sí y 6 no

`muci-integrador/__init__.py` hace `pymysql.install_as_MySQLdb()`. PyMySQL **hardcodea** una
versión falsa para hacerse pasar por MySQLdb:

```python
VERSION = (1, 1, 2, "final")          # versión real del paquete
version_info = (1, 4, 6, "final", 1)  # spoof fijo en el código
```

Y el backend MySQL de Django compara contra ese `version_info`:

| destino | umbral | spoof | ¿arranca? |
|---|---|---|---|
| **Django 5.2** | `≥ (1, 4, 3)` | `(1, 4, 6)` | **sí, sin cambiar de driver** |
| Django 6 | `≥ (2, 2, 1)` | `(1, 4, 6)` | no |

Es un `ImproperlyConfigured` al arrancar, o sea que se manifestaría recién al reiniciar el
servicio. Confirma que **5.2 es el destino correcto** y marca el trabajo que exigiría el
salto siguiente.

### 6. El hueco de validación, que es el hallazgo más incómodo

`test_settings.py` carga **cuatro apps**: `contenttypes`, `auth`, `rest_framework`, `core`.
Los 176 tests verdes sobre Django 6 prueban que la lógica de `core` y DRF andan — y **no**
prueban `drf_yasg`, `corsheaders`, `django.contrib.admin`, ni que el `settings.py` real
siquiera importe.

Dicho de otro modo: **hoy no existe ningún entorno que corra el settings real sobre Django
moderno.** El venv local ni tiene `corsheaders` instalado. Ese hueco es exactamente donde
vive el riesgo del upgrade, y cerrarlo es una fase del plan.

## Diseño: enfoque A, venv paralelo y flip

Construir un venv **nuevo en otra ruta**, validarlo con producción corriendo normal, y
cambiar **una línea** del unit de systemd. El venv viejo queda intacto.

Los dos enfoques descartados: escalonar 3.2 → 4.2 → 5.2 pasa por una versión **también
vencida** y muta el venv vivo en cada paso (sin rollback limpio en ningún momento); y
reconstruir el mismo venv directo destruye el único rollback barato que hay.

### Fase 0 — Backup verificado (solo lee)

Primer backup que van a tener estas bases. Cuatro: `muci` (2,4 GB), `krayin` (129 MB),
`moodle` (23 MB), `muci-integrador` (6 MB).

Se usa el usuario de solo lectura, porque **la credencial de root de MariaDB no está a mano**
(`/root/.my.cnf` no existe y `debian.cnf` no se honra):

```bash
set -o pipefail
MYSQL_PWD="$PASS" mariadb-dump -u anthropic_readonly \
    -B muci muci-integrador krayin moodle \
    --single-transaction --quick --no-tablespaces \
  | gzip > /root/bk/db-pre-django52.sql.gz
```

- `--no-tablespaces` es obligatorio: ese usuario no tiene el privilegio `PROCESS`.
- `set -o pipefail` no es opcional: con el pipe a `gzip` el `$?` refleja el gzip y **miente**.
  Ya pasó — reportó éxito sobre un gzip vacío de 20 bytes.
- **Criterio de aceptación:** la última línea del dump descomprimido dice
  `-- Dump completed on ...`, y el tamaño es del orden de magnitud esperado.

**Insumo que falta:** la contraseña de `anthropic_readonly` para MariaDB. El clasificador de
permisos bloquea que la lea del `.env` de producción, así que Carlos la pasa por el entorno.

### Fase 1 — Venv paralelo (no toca producción)

```bash
/usr/bin/python3.10 -m venv /root/venv-integrador-52
```

**Deliberadamente sin `pipenv`:** pipenv deriva la ruta del venv del hash del directorio del
proyecto y reusaría `integrador-ObaHlHmv`, que es precisamente el que hay que conservar.

Cambios de dependencias sobre la línea base de producción:

| paquete | de | a | por qué |
|---|---|---|---|
| `Django` | 3.2.25 | 5.2.x | el objetivo |
| `djangorestframework` | 3.15.1 | ≥3.16 | ahí entra el soporte de Django 5.2 |
| `drf-yasg` | 1.21.8 | 1.21.15 | verificado: importa en Django 6 |
| `django-cors-headers` | 4.1.0 | al día | 4.1 no declara soporte de 5.2 |
| `backports.zoneinfo` | 0.2.1 | **fuera** | shim exclusivo de Python 3.7 |
| `pytz` | 2025.1 | **fuera** | Django ya no lo necesita |
| `djangorestframework-simplejwt` | 5.3.0 | **fuera, a confirmar** | cero imports, cero menciones en settings |
| `mysqlclient` | 2.1.1 | **fuera, a confirmar** | lo tapa el shim de PyMySQL |

Las dos bajas marcadas "a confirmar" se validan quitándolas y viendo que la fase 2 siga verde;
si algo las necesita, se quedan y se anota por qué.

El `Pipfile` se actualiza en la rama para que refleje esto, aunque el venv nuevo no se cree
con pipenv: es la fuente de verdad declarativa del proyecto.

**Cierre de fase:** 176/176 con `test_settings` en el venv nuevo, y `showmigrations --plan`
con el settings real contra la base viva confirmando que no hay nada por aplicar.

### Fase 2 — Cerrar el hueco de validación (no toca producción)

Con TDD, en la rama. Es la fase que da valor más allá del upgrade.

1. **Un test que cargue el `settings.py` real y corra `check`.** Es el único que habría
   atrapado el `ImproperlyConfigured` del driver **antes** de reiniciar el servicio, en vez
   de descubrirlo con producción caída. Necesita resolver que el settings real lee `.env` con
   `dotenv_values()` en el import: el test provee un `.env` de prueba o parchea la carga.
2. **Ampliar `test_settings`** a `admin`, `sessions`, `messages`, `staticfiles`, `drf_yasg` y
   `corsheaders`. Puede romper tests existentes; se arregla en esta fase.
3. **Smoke test del endpoint de Swagger** (`drf_yasg` generando el schema, no solo importando)
   y **del changelist de `FailedOrderAdmin`**, que hoy la suite no toca y que se modificó el
   2026-08-28.

### Fase 3 — El flip (única fase que toca producción)

1. Backup del unit actual.
2. `ExecStart` (y cualquier ruta al intérprete en el unit) al venv nuevo.
3. `systemctl daemon-reload` + `systemctl restart mucintegrador.service`.
4. `migrate` — esperado no-op; si aparece algo por aplicar, **parar** y revisar.
5. Verificación: `is-active`, `ActiveEnterTimestamp` contra el `mtime` del código, el
   endpoint de Swagger, el admin, y una venta real end-to-end.

**Rollback:** revertir la línea del unit, `daemon-reload`, restart. Segundos, y sin estado de
base que deshacer.

**Ventana:** evitar 00/06/12/18 UTC, que son los reinicios automáticos del cron, y en
particular las 00:00 UTC, cuando corre `sync_bims_contacts` y hace 38 llamadas secuenciales.

### Fase 4 — Local a 5.2 (no toca producción)

Reconstruir `.venv` con Django 5.2 y las mismas versiones de dependencias que producción,
sobre el Python 3.12 que ya hay. A partir de ahí, un verde local significa algo otra vez, y
`verificar-en-stack-produccion.sh` pasa de única red a confirmación del intérprete.

## Riesgos

| riesgo | mitigación |
|---|---|
| `drf_yasg` importa pero falla al **generar** el schema | Fase 2, punto 3: el smoke test lo ejercita de verdad |
| DRF 3.16 cambia comportamiento de `APIView`/`Response` | Los 176 tests cubren las vistas; la fase 2 amplía |
| El unit tiene rutas al venv en más lugares que `ExecStart` | Fase 3 paso 1: leer el unit completo antes de editar |
| Aparece una migración inesperada en el `migrate` | Paso 4 corta el deploy; el backup de la fase 0 cubre |
| El venv nuevo llena el disco | Medir espacio antes; el viejo se borra recién semanas después |

## Fuera de alcance y por qué

- **Django 6:** exige salir del shim de PyMySQL hacia `mysqlclient`, que necesita build deps
  del sistema. Se decide cuando 5.2 esté estable.
- **Borrar el venv viejo:** se conserva como rollback. Se borra cuando haya confianza, no en
  este proyecto.
- **`USE_L10N`:** se saca porque está al lado, pero no es parte del objetivo.

## Tests

- Fase 1: 176/176 con `test_settings` en el venv nuevo.
- Fase 2: los tres tests nuevos, cada uno visto en rojo antes de implementarse.
- Fase 2: la suite ampliada sigue verde con las apps agregadas.
- Fase 3: `verificar-en-stack-produccion.sh` sobre el commit final, más la corrida manual
  sobre el venv nuevo (la receta documentada en la memoria del proyecto).

## Criterios de éxito

1. `mucintegrador.service` corriendo sobre Python 3.10 + Django 5.2 LTS.
2. `migrate` no-op, confirmado en el momento del flip.
3. Una venta real facturada después del flip, con su `bims_sale_id` y su meta en WooCommerce.
4. El venv viejo intacto y el rollback probado **antes** de necesitarlo.
5. Un backup verificado de las cuatro bases, que antes no existía.
6. Local en 5.2, y algo en la suite que cargue el settings real.
