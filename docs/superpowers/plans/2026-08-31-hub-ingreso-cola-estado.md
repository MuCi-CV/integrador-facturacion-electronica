# Sub-proyecto A — Ingreso, cola y estado por rama — Plan de implementación

> **Para workers agénticos:** SUB-SKILL REQUERIDA: usar `superpowers:subagent-driven-development`
> (recomendado) o `superpowers:executing-plans` para implementar tarea por tarea. Los pasos usan
> checkbox (`- [ ]`) para seguimiento.

**Goal:** Que toda transacción quede persistida antes de procesarse, con estado explícito por rama
de salida, y que el ingreso no pueda hacer que WooCommerce apague el webhook de facturación.

**Architecture:** `SalesView` pasa a persistir y devolver `202`. Un management command por cron
(cada minuto, con `flock`) toma las filas pendientes con `SELECT … FOR UPDATE SKIP LOCKED` y hace
el trabajo real. `FailedOrder` se extiende en su lugar para ser a la vez cola y tabla de estado.

**Tech Stack:** Django 5.2.17, MariaDB 12.3.2, cron + `flock`, Slack Incoming Webhook.

**Spec:** `docs/superpowers/specs/2026-08-31-hub-ingreso-cola-estado-design.md`

---

## Global Constraints

- **Compatibilidad Python 3.7.** El modo por defecto de `./verificar-en-stack-produccion.sh` corre
  con el `python3.7` del sistema (Django 3.2.18) y es el stack de **rollback**. Sintaxis exclusiva
  de 3.8+ (walrus, `match`, `X | Y` en anotaciones, `dict|dict`) pone ese chequeo en rojo y **se
  pierde la red de seguridad**. Usar `Optional[X]` / `Dict[str, Any]` de `typing`.
- **Tests:** `.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings`.
- **Sin tráfico real** a BIMS ni a WooCommerce en los tests: `unittest.mock` / `responses`.
- **Anotaciones de tipo obligatorias** en funciones nuevas. **Black** para el formato.
- **Nomenclatura (Carlos, 2026-08-31):** **identificadores de código y columnas de BD en inglés**
  (`external_reference`, `origin`, `woo_meta_ok`); **documentación, comentarios, docstrings y
  `verbose_name` en español**. Los nombres de test siguen en español, como el resto del archivo.
- 🔒 **Acceso al servidor:** los `ssh` del asistente van **siempre** como `anthropic_readonly@muci.org`.
  **Todo comando que modifique el servidor se le pasa a Carlos** para que lo corra él con `!`.
- 🔒 **`git add` por nombre, nunca `git add -A`.** El 2026-08-31 un `git add -A` mandó tres archivos
  de dev a producción.
- 🔒 **Todo comando de parcheo sobre el servidor debe ser idempotente** o chequear antes de escribir.
  El 2026-08-31 un `sed` corrido dos veces duplicó una directiva de nginx.
- 🔒 **Los viernes no se despliega.**

---

## Estructura de archivos

| archivo | responsabilidad | tarea |
|---|---|---|
| `core/models.py` | `FailedOrder`: estados, identidad, campos de cola | 1, 2 |
| `core/migrations/0009_*.py` | estados y campos nuevos (aditivo) | 1 |
| `core/migrations/0010_*.py` | `external_reference` + `unique` + backfill (expansión) | 2 |
| `core/states.py` | **nuevo** — helpers de transición, sin dependencias de red | 4 |
| `core/views.py` | `SalesView`: validar, persistir, `202` | 5 |
| `core/services.py` | `process_order`: marcar `NOT_APPLICABLE`/`PAUSED`; sin cambios de negocio | 4, 7 |
| `core/management/commands/process_queue.py` | **nuevo** — worker + reaper | 6 |
| `core/alerts.py` | **nuevo** — cliente de Slack con throttling | 8 |
| `core/admin.py` | colores y filtros de los estados nuevos; **y los dos reintentos por HTTP** | 1, 2, **5** |
| `core/management/commands/retryfaileds.py` | reencolar por BD, no por HTTP | 5 |
| `core/management/commands/sync_bims_contacts.py` | leer el estado `PAUSED`, reencolar por BD | 4, 5 |
| `process-queue.sh` | **nuevo** — envoltorio con `flock` para el cron | 6 |

**Decisión de secuencia:** todo el trabajo de **esquema** (Tareas 1-2) va primero y se despliega
**sin cambiar comportamiento** (Tarea 3). Recién después va el cambio de contrato.

Y el esquema se hace **expandiendo, no renombrando**: `order_id` sobrevive intacto y la columna
nueva convive con él. Borrarla es una tarea diferida (3-bis) que no bloquea nada. Así ningún
despliegue de este plan puede dejar la tabla fiscal a mitad de camino.

---

### ⚠️ Hallazgo previo al plan: CUATRO consumidores se rompen con el 202

**Corregido el 2026-08-31: son cuatro, no dos.** Los otros dos aparecieron al leer `admin.py`
completo, y son los más fáciles de pasar por alto porque están dentro de métodos del admin y no en
un comando con nombre evidente.

| archivo | qué es | línea del `status_code == 200` |
|---|---|---|
| `core/management/commands/retryfaileds.py` | comando de reintentos, lo llama `runretryfaileds.sh` | `:28` |
| `core/management/commands/sync_bims_contacts.py` | reintento de las pausadas, dentro del sync | `:78` |
| `core/admin.py` → `retry_failed_orders_button` | **botón del admin** "reintentar fallidas" | `:111` |
| `core/admin.py` → `retry_selected_orders` | **acción del admin** sobre la selección | `:152` |

Los cuatro hacen `POST` a `/sales/` y chequean **`response.status_code == 200`**. Con el `202` esa
condición **nunca vuelve a ser cierta**: no explotan, simplemente **dejan de marcar nada** y el
reintento se vuelve un no-op **silencioso**. Los dos del admin son peores en un aspecto: alguien
aprieta el botón, no ve error, y asume que reintentó.

Además, con la cola, reencolar por HTTP contra nosotros mismos deja de tener sentido: es escribir
`PENDING` en una fila. **La Tarea 5 convierte los cuatro a escritura directa en BD.** No es
opcional.

---

## Task 1: Estados y campos nuevos (aditivo, sin cambiar comportamiento)

**Files:**
- Modify: `core/models.py` (clase `FailedOrder`)
- Create: `core/migrations/0009_estados_y_campos_de_cola.py` (generada)
- Modify: `core/admin.py:39-48` (`colored_status`), `:30` (`list_filter`)
- Test: `core/tests.py`

**Interfaces:**
- Produces: `FailedOrder.PENDING=3`, `PROCESSING=4`, `PAUSED=5`, `NOT_APPLICABLE=6`; campos
  `origin`, `bims_attempts`, `bims_next_attempt`, `woo_meta_ok`, `claimed_at`.

- [ ] **Step 1: Escribir el test que falla**

```python
class EstadosDeColaTest(TestCase):
    def test_los_estados_existentes_conservan_su_valor(self):
        """8588 filas en produccion dependen de estos dos numeros."""
        self.assertEqual(FailedOrder.FAILED, 1)
        self.assertEqual(FailedOrder.COMPLETED, 2)

    def test_los_estados_nuevos_existen_con_sus_valores(self):
        self.assertEqual(FailedOrder.PENDING, 3)
        self.assertEqual(FailedOrder.PROCESSING, 4)
        self.assertEqual(FailedOrder.PAUSED, 5)
        self.assertEqual(FailedOrder.NOT_APPLICABLE, 6)

    def test_los_campos_de_cola_tienen_defaults_seguros(self):
        f = FailedOrder.objects.create(order_id=1)
        self.assertEqual(f.origin, "woo")
        self.assertEqual(f.bims_attempts, 0)
        self.assertIsNone(f.bims_next_attempt)
        self.assertFalse(f.woo_meta_ok)
        self.assertIsNone(f.claimed_at)
```

- [ ] **Step 2: Correr y verificar que falla**

Run: `.venv/bin/python manage.py test core.tests.EstadosDeColaTest --settings=muci-integrador.test_settings`
Expected: FAIL con `AttributeError: type object 'FailedOrder' has no attribute 'PENDING'`

- [ ] **Step 3: Implementar en el modelo**

En `core/models.py`, dentro de `FailedOrder`, reemplazar el bloque de estados:

```python
    FAILED = 1
    COMPLETED = 2
    # Los dos de arriba YA EXISTEN en produccion con esos valores: 8588 filas
    # dependen de ellos. Los nuevos se agregan arriba, nunca renumerando.
    PENDING = 3
    PROCESSING = 4
    PAUSED = 5
    NOT_APPLICABLE = 6
    STATUS_CHOICES = (
        (FAILED, "Fallido"),
        (COMPLETED, "Completado"),
        (PENDING, "Pendiente"),
        (PROCESSING, "En proceso"),
        (PAUSED, "Pausada"),
        (NOT_APPLICABLE, "No aplica"),
    )

    ORIGIN_WOO = "woo"
    ORIGIN_CRM = "crm"  # sin uso en el sub-proyecto A; lo estrena F
    ORIGIN_CHOICES = ((ORIGIN_WOO, "WooCommerce"), (ORIGIN_CRM, "CRM Krayin"))
```

Y los campos nuevos, después de `bims_invoice_number`:

```python
    origen = models.CharField(
        verbose_name="Origen",
        max_length=8,
        choices=ORIGIN_CHOICES,
        default=ORIGIN_WOO,
        db_index=True,
    )
    bims_attempts = models.PositiveSmallIntegerField(
        verbose_name="Intentos contra BIMS", default=0
    )
    bims_next_attempt = models.DateTimeField(
        verbose_name="Próximo intento", null=True, blank=True, db_index=True
    )
    # La rama de anotar en WooCommerce no lleva backoff propio: es una llamada
    # barata e idempotente, y le alcanza con reintentarse en cada pasada. Ver
    # §5.2 de la spec.
    woo_meta_ok = models.BooleanField(verbose_name="Anotada en WooCommerce", default=False)
    claimed_at = models.DateTimeField(
        verbose_name="Tomada por el worker", null=True, blank=True
    )
```

- [ ] **Step 4: Generar la migración y correr los tests**

⚠️ **`makemigrations` va con `dev_settings`, NO con `test_settings`.** `core/bims.py:723` tiene
`bims = BimsApi()` **a nivel de módulo** y el `__init__` hace login, así que cualquier `manage.py`
con `test_settings` intenta conectarse a `bims.test.local` y **crashea sin generar nada**.
Verificado el 2026-08-31. `dev_settings.py` existe justo para esto (está gitignorado, es
herramienta local).

```bash
.venv/bin/python manage.py makemigrations core --settings=muci-integrador.dev_settings
.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings
```
Expected: migración `0009_*` creada; **183 tests + 3 nuevos = 186, OK**.

Renombrar el archivo generado a `0009_estados_y_campos_de_cola.py`: el nombre automático
(`0009_failedorder_bims_attempts_and_more.py`) no dice qué hace.

- [ ] **Step 5: Actualizar el admin para que los estados nuevos se vean**

En `core/admin.py`, reemplazar el diccionario de colores hardcodeado:

```python
    def colored_status(self, obj):
        colors = {
            FailedOrder.FAILED: "red",
            FailedOrder.COMPLETED: "green",
            FailedOrder.PENDING: "#F37043",
            FailedOrder.PROCESSING: "#6950A1",
            FailedOrder.PAUSED: "#F17DB1",
            FailedOrder.NOT_APPLICABLE: "gray",
        }
```

Y agregar `origin` al filtro: `list_filter = ("status", "origin")`.

- [ ] **Step 6: Commit**

```bash
git add core/models.py core/admin.py core/tests.py core/migrations/0009_estados_y_campos_de_cola.py
git commit -m "feat(cola): estados y campos de cola en FailedOrder

Aditivo: FAILED=1 y COMPLETED=2 conservan su valor porque 8588 filas de
produccion dependen de ellos. Sin cambios de comportamiento todavia."
```

---

## Task 2: Identidad — expandir (agregar `external_reference`, sin tocar `order_id`)

> ⚠️ **Esta tarea reemplaza al `RenameField` del plan original.** Ver §"Por qué expandir y
> contraer" abajo. El cambio se decidió el 2026-08-31, antes de implementarla.

**Files:**
- Modify: `core/models.py`, `core/services.py`, `core/admin.py`
- Create: `core/migrations/0010_external_reference.py`
- Test: `core/tests.py`

**Interfaces:**
- Produces: `FailedOrder.external_reference: str` (nullable en esta tarea),
  `unique_together = ("origin", "external_reference")`.

### Por qué expandir y contraer, y no renombrar

`RenameField` genera un `ALTER TABLE … CHANGE`, que conserva los datos. El problema no es la
operación en sí, son dos cosas del contexto:

1. **En MySQL/MariaDB el DDL hace commit implícito.** No se deshace dentro de una transacción, así
   que la atomicidad que Django le da a una migración **no cubre los cambios de esquema**. Una
   migración con rename + cambio de tipo + `unique` + dos migraciones de datos que falla en la
   cuarta operación deja la base a mitad de camino, y no vuelve sola.
2. **No hay dump a mano para ensayarla** (confirmado con Carlos el 2026-08-31), así que el paso
   "probar contra una copia de los datos reales" no se puede cumplir.

Expandir y contraer nunca deja un estado intermedio peligroso: **`order_id` sigue intacto todo el
tiempo**, y el rollback de cada paso es volver el código, sin tocar la base.

- [ ] **Step 1: Escribir los tests que fallan**

```python
class IdentidadPorOrigenTest(TestCase):
    def test_la_misma_referencia_en_origenes_distintos_convive(self):
        FailedOrder.objects.create(
            order_id=204000, external_reference="204000", origin=FailedOrder.ORIGIN_WOO
        )
        FailedOrder.objects.create(
            order_id=204000, external_reference="204000", origin=FailedOrder.ORIGIN_CRM
        )
        self.assertEqual(FailedOrder.objects.count(), 2)

    def test_la_misma_referencia_en_el_mismo_origen_no_se_duplica(self):
        FailedOrder.objects.create(
            order_id=204000, external_reference="204000", origin=FailedOrder.ORIGIN_WOO
        )
        with self.assertRaises(IntegrityError):
            FailedOrder.objects.create(
                order_id=204000, external_reference="204000", origin=FailedOrder.ORIGIN_WOO
            )

    def test_escribir_por_services_llena_las_dos_columnas(self):
        """
        Durante la expansion las dos conviven: `order_id` es la fuente de verdad
        heredada y `external_reference` la nueva. Escribir solo una dejaria
        filas que el codigo viejo o el nuevo no puede encontrar.
        """
        from core.states import upsert_state

        fila = upsert_state("204000", status=FailedOrder.PENDING)
        self.assertEqual(fila.order_id, 204000)
        self.assertEqual(fila.external_reference, "204000")
```

- [ ] **Step 2: Correr y verificar que fallan**

Run: `.venv/bin/python manage.py test core.tests.IdentidadPorOrigenTest --settings=muci-integrador.test_settings`
Expected: FAIL con `TypeError: FailedOrder() got unexpected keyword arguments: 'external_reference'`

- [ ] **Step 3: Agregar el campo, nullable**

```python
    # Fase de EXPANSION: convive con `order_id`, que sigue siendo la columna
    # heredada. Nullable a proposito — las 8588 filas existentes se llenan por
    # migracion de datos, y hasta que eso corra tiene que poder estar vacia.
    # La contraccion (borrar `order_id`) es una tarea aparte y posterior.
    external_reference = models.CharField(
        verbose_name="Referencia externa",
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )
```

En `class Meta`: `unique_together = ("origin", "external_reference")`.

⚠️ En MariaDB un `UNIQUE` permite **múltiples NULL**, así que el constraint no molesta mientras las
filas viejas estén sin llenar.

- [ ] **Step 4: Generar la migración y agregarle el backfill**

```bash
.venv/bin/python manage.py makemigrations core --settings=muci-integrador.dev_settings
```

⚠️ **Con `dev_settings`, no con `test_settings`:** `core/bims.py:723` tiene `bims = BimsApi()` a
nivel de módulo y el `__init__` hace login, así que `manage.py` con `test_settings` intenta
conectarse a un host inventado y **crashea**. Verificado el 2026-08-31.

Agregar a mano el backfill a la migración generada:

```python
def llenar_external_reference(apps, schema_editor):
    """Copia, no movimiento: `order_id` queda intacto."""
    FailedOrder = apps.get_model("core", "FailedOrder")
    for fila in FailedOrder.objects.filter(external_reference__isnull=True).iterator():
        fila.external_reference = str(fila.order_id)
        fila.save(update_fields=["external_reference"])


def vaciar_external_reference(apps, schema_editor):
    apps.get_model("core", "FailedOrder").objects.update(external_reference=None)
```

Y las dos migraciones de datos que ya estaban previstas: `woo_meta_ok = True` para las `COMPLETED`
anteriores al 2026-08-28, y las `FAILED` con `message` que empieza con `"Pausada: Esperando"` →
`PAUSED`. Las tres con su función inversa.

- [ ] **Step 5: Escribir en las dos columnas**

En `core/states.py`, un único punto de escritura para que ningún call site pueda llenar una sola:

```python
def upsert_state(referencia: str, **defaults) -> FailedOrder:
    """
    Unico lugar que escribe la identidad, para que sea imposible llenar una
    columna y no la otra durante la expansion.
    """
    fila, _ = FailedOrder.objects.update_or_create(
        origin=FailedOrder.ORIGIN_WOO,
        external_reference=str(referencia),
        defaults=dict(defaults, order_id=int(referencia)),
    )
    return fila
```

Migrar los `update_or_create(order_id=...)` de `services.py` a este helper.

- [ ] **Step 6: Correr la suite**

Expected: **189 OK**.

- [ ] **Step 7: Commit**

```bash
git add core/models.py core/services.py core/admin.py core/states.py core/tests.py \
        core/migrations/0010_external_reference.py
git commit -m "feat(cola): external_reference por origen, en fase de expansion

Se agrega la columna nueva y se llena por backfill; order_id queda intacto. No
se usa RenameField: en MariaDB el DDL hace commit implicito, asi que una
migracion de varias operaciones que falla a mitad no vuelve sola, y no hay dump
a mano para ensayarla."
```

---

## Task 3: 🚀 DESPLIEGUE 1 — solo esquema, comportamiento idéntico

**Files:** ninguno. Es un despliegue.

**Qué se despliega:** las migraciones `0009` (aditiva) y `0010` (columna nueva + backfill). El
comportamiento no cambia: `/sales/` sigue devolviendo 200 y facturando igual.

**Por qué es mucho menos riesgoso que antes:** ninguna columna se renombra ni se borra, y
`order_id` sigue siendo la fuente de verdad. Si el backfill sale mal, se vuelve a correr — es
idempotente porque filtra por `external_reference__isnull=True`.

- [ ] **Step 1: Verificar sobre el stack de rollback (lo corre el asistente)**

```bash
./verificar-en-stack-produccion.sh
```
Expected: `VERDE`, 189 tests.

- [ ] **Step 2: Verificar sobre el stack REAL (lo corre Carlos)**

```
! PYTHON=/root/venv-integrador-52/bin/python SERVIDOR=root@muci.org REMOTO=wt-verificacion-52 ./verificar-en-stack-produccion.sh
```
Expected: `Python 3.10.12 | Django 5.2.17`, 189 OK.

- [ ] **Step 3: Backup inmediatamente antes (lo corre Carlos)**

```
! ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org 'cd /var/www/integrador && MYSQL_PWD=<pass> ./backup-bases.sh pre-expansion'
```
Expected: las 4 bases con `Dump completed on`. **Sin esto no se sigue** — y de paso deja el dump
que hoy no tenemos para ensayar la contracción más adelante.

- [ ] **Step 4: Ver el plan de migración sin aplicarlo (Carlos)**

```
! ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org 'cd /var/www/integrador && /root/venv-integrador-52/bin/python manage.py migrate core --plan'
```
Expected: exactamente `0009` y `0010` sin aplicar, nada más.

- [ ] **Step 5: Conteos ANTES (Carlos)**

```
! ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org 'cd /var/www/integrador && /root/venv-integrador-52/bin/python manage.py shell -c "
import sentry_sdk; sentry_sdk.get_global_scope().set_client(None)
from core.models import FailedOrder
from django.db.models import Count
print(list(FailedOrder.objects.values(\"status\").annotate(n=Count(\"id\")).order_by(\"status\")))
print(\"total:\", FailedOrder.objects.count())
"'
```
Anotar la salida.

- [ ] **Step 6: Desplegar (Carlos)**

```
! ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org 'cd /var/www/integrador && git pull --ff-only 2>&1 | tail -3 && git log --oneline -1 && /root/venv-integrador-52/bin/python manage.py migrate core 2>&1 | tail -5 && systemctl restart mucintegrador.service && sleep 5 && systemctl is-active mucintegrador.service'
```

- [ ] **Step 7: Verificar el backfill (Carlos)**

Total de filas idéntico al Step 5; **0 filas con `external_reference` nulo**; y para una muestra,
`str(order_id) == external_reference`.

- [ ] **Step 8: Confirmar que el comportamiento NO cambió**

Esperar una venta real: `POST /sales/` → **200**, `FailedOrder` en `COMPLETED` con `bims_sale_id`,
metas en la orden de Woo. **Y las dos columnas de identidad llenas.**

**Rollback:** `git reset` al commit anterior y `systemctl restart`. **Las migraciones se pueden
dejar aplicadas**: una columna nueva que el código viejo ignora es inofensiva. Eso es justamente la
ventaja de expandir antes de contraer.

---

## Task 3-bis (DIFERIDA): contraer — borrar `order_id`

**No forma parte de este plan.** Se hace cuando las dos columnas lleven semanas coincidiendo en
producción y exista un dump con el que ensayar. Es el único paso sin vuelta atrás, y no bloquea
nada: `order_id` de más no molesta a nadie.

Requisitos para abrirla: dump reciente restaurable, verificación de que ningún consumidor lee
`order_id`, y una ventana sin despliegues encima.

---

## Task 4: Los estados nuevos se usan de verdad (todavía sin async)

**Files:**
- Create: `core/states.py`
- Modify: `core/services.py` (las tres ramas de retorno temprano)
- Test: `core/tests.py`

**Interfaces:**
- Produces: `core.states.mark_not_applicable(referencia: str, motivo: str) -> None`

**Por qué antes del 202:** cierra la ambigüedad de la spec §1.3 **sin** tocar el contrato HTTP, así
que si algo sale mal se sabe que fue esto y no el async.

- [ ] **Step 1: Escribir el test que falla**

```python
class NoAplicaTest(TestCase):
    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_una_orden_de_monto_cero_deja_fila_en_no_aplica(
        self, mock_wc, mock_bims, _c
    ):
        """
        Hoy estas ordenes NO dejan fila ninguna, y por eso "no esta la meta"
        podia significar "no correspondia facturar". Ver spec §1.3.
        """
        from core.services import process_order

        orden = self._order()
        orden["total"] = "0"
        mock_wc.get_order.return_value = orden

        process_order(order_id=202707)

        f = FailedOrder.objects.get(external_reference="202707")
        self.assertEqual(f.status, FailedOrder.NOT_APPLICABLE)
        self.assertIn("Monto 0", f.message)
        mock_bims.create_sale.assert_not_called()
```

- [ ] **Step 2: Correr y verificar que falla**

Expected: FAIL con `FailedOrder.DoesNotExist` — que es exactamente el bug.

- [ ] **Step 3: Crear el helper**

`core/states.py`:

```python
"""
Transiciones de estado de `FailedOrder`, sin dependencias de red.

Vive aparte de `services.py` a proposito: `services` importa `core.bims`, que
instancia `BimsApi()` en el import y **dispara un login real contra BIMS**. Un
helper de estado no tiene por que arrastrar eso.
"""
from typing import Optional

from core.models import FailedOrder


def mark_not_applicable(referencia: str, motivo: str) -> None:
    """La transaccion no corresponde facturar. Estado terminal, sin reintento."""
    FailedOrder.objects.update_or_create(
        external_reference=str(referencia),
        origin=FailedOrder.ORIGIN_WOO,
        defaults={"status": FailedOrder.NOT_APPLICABLE, "message": motivo},
    )
```

- [ ] **Step 4: Usarlo en las tres ramas de retorno temprano de `services.py`**

Reemplazar cada `return {"status": ...}` temprano por una llamada previa:

```python
        mark_not_applicable(order_id, "Descuento 100%" if discount > 0 else "Monto 0")
        return {"status": "Descuento 100%" if discount > 0 else "Monto 0"}
```

Idem para `"No procesado"` y `"Productos en 0"`.

- [ ] **Step 5: Correr los tests**

Expected: **189 OK**.

- [ ] **Step 6: Commit**

```bash
git add core/states.py core/services.py core/tests.py
git commit -m "feat(cola): las ordenes que no corresponde facturar dejan fila NOT_APPLICABLE"
```

---

## Task 5: El ingreso devuelve 202 y persiste

⚠️ **Cambia el contrato con WooCommerce.** No desplegar sin la Tarea 6: sin worker, nada se procesa.

**Files:**
- Modify: `core/views.py` (`SalesView.post`), `core/states.py`
- Modify (los CUATRO consumidores del 202): `core/management/commands/retryfaileds.py`,
  `core/management/commands/sync_bims_contacts.py`, y en `core/admin.py` los métodos
  `retry_failed_orders_button` y `retry_selected_orders`
- Test: `core/tests.py`

**Interfaces:**
- Produces: `core.states.enqueue(referencia: str, origen: str = "woo") -> FailedOrder`

- [ ] **Step 1: Escribir los tests que fallan**

```python
class IngresoAsincronoTest(TestCase):
    @patch("core.views.process_order")
    def test_el_ingreso_responde_202_y_no_procesa_nada(self, mock_process):
        r = self.client.post("/sales/", {"arg": 204000}, format="json")
        self.assertEqual(r.status_code, 202)
        mock_process.assert_not_called()
        self.assertEqual(
            FailedOrder.objects.get(external_reference="204000").status,
            FailedOrder.PENDING,
        )

    def test_sin_referencia_sigue_siendo_400(self):
        self.assertEqual(self.client.post("/sales/", {}, format="json").status_code, 400)

    def test_una_reentrega_de_orden_completada_no_la_reencola(self):
        """Ya se facturo: reprocesar es riesgo sin beneficio. Spec §4."""
        FailedOrder.objects.create(
            external_reference="204000", status=FailedOrder.COMPLETED, bims_sale_id="31385"
        )
        self.client.post("/sales/", {"arg": 204000}, format="json")
        self.assertEqual(
            FailedOrder.objects.get(external_reference="204000").status,
            FailedOrder.COMPLETED,
        )

    def test_una_reentrega_de_orden_fallida_la_reencola(self):
        FailedOrder.objects.create(external_reference="204000", status=FailedOrder.FAILED)
        self.client.post("/sales/", {"arg": 204000}, format="json")
        self.assertEqual(
            FailedOrder.objects.get(external_reference="204000").status,
            FailedOrder.PENDING,
        )
```

- [ ] **Step 2: Correr y verificar que fallan**

Expected: FAIL — el primero con `202 != 200`.

- [ ] **Step 3: Implementar `enqueue`**

En `core/states.py`:

```python
# Estados desde los que una re-entrega vuelve a encolar. COMPLETED queda afuera
# (ya se facturo) y PENDING/PROCESSING tambien (ya esta en la cola). Spec §4.
REQUEUEABLE = (FailedOrder.FAILED, FailedOrder.NOT_APPLICABLE)


def enqueue(referencia: str, origen: str = FailedOrder.ORIGIN_WOO) -> FailedOrder:
    fila, creada = FailedOrder.objects.get_or_create(
        external_reference=str(referencia),
        origin=origen,
        defaults={"status": FailedOrder.PENDING, "message": "Encolada."},
    )
    if not creada and fila.status in REQUEUEABLE:
        fila.status = FailedOrder.PENDING
        fila.message = "Reencolada."
        fila.bims_attempts = 0
        fila.bims_next_attempt = None
        fila.save(update_fields=["status", "message", "bims_attempts", "bims_next_attempt"])
    return fila
```

- [ ] **Step 4: Reemplazar el cuerpo de `SalesView.post`**

```python
class SalesView(APIView):
    def post(self, request):
        order_id = request.data.get("arg")
        if not order_id:
            logger.error("No se recibió 'order_id' en la solicitud.")
            return Response(
                data={"status": "fail", "error": "No se recibió 'order_id'"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Persistir y salir. El trabajo lo hace `process_queue`.
        # Devolver 202 SIEMPRE es deliberado: Woo deshabilita el webhook a las 5
        # respuestas no-2xx seguidas, y ya mato asi al webhook `Refund order`.
        enqueue(order_id)
        return Response(data={"status": "encolada"}, status=status.HTTP_202_ACCEPTED)
```

- [ ] **Step 5: Convertir los CUATRO reintentos a escritura directa en BD**

**Crítico y fácil de subestimar:** los cuatro chequean `status_code == 200`, que con el `202` nunca
vuelve a ser cierto. No fallan con error — **dejan de hacer nada, sin avisar**.

**5a — `core/management/commands/retryfaileds.py`.** Reemplazar todo el bucle HTTP por:

```python
        from core.states import enqueue

        reencoladas = 0
        for orden in FailedOrder.objects.filter(status=FailedOrder.FAILED):
            enqueue(orden.external_reference, orden.origin)
            reencoladas += 1
        self.stdout.write(self.style.SUCCESS(f"Reencoladas: {reencoladas}"))
```

**5b — `core/management/commands/sync_bims_contacts.py`.** El mismo patrón, filtrando
`status=FailedOrder.PAUSED`. Se van los `import requests` que quedan sin uso en ese bloque.

**5c — `core/admin.py`, `retry_failed_orders_button` (`:98-137`).** Todo el cuerpo del `try` se
reemplaza por el mismo bucle, y el mensaje al usuario tiene que **decir la verdad nueva**: ya no
"procesadas", sino "encoladas".

```python
    def retry_failed_orders_button(self, request):
        """Reencola todas las fallidas. El worker las procesa en ~1 minuto."""
        from core.states import enqueue

        ordenes = FailedOrder.objects.filter(status=FailedOrder.FAILED)
        for orden in ordenes:
            enqueue(orden.external_reference, orden.origin)
        self.message_user(
            request,
            f"{len(ordenes)} orden(es) reencolada(s). Se procesan en el próximo "
            f"minuto; la pantalla no cambia al instante.",
            level=messages.SUCCESS,
        )
        return HttpResponseRedirect("..")
```

**5d — `core/admin.py`, `retry_selected_orders` (`:139-176`).** Igual, sobre el `queryset`,
conservando el filtro por `FAILED` que ya tenía.

⚠️ **El mensaje importa tanto como el código.** Estos dos son botones que aprieta una persona: si
dicen "procesadas correctamente" cuando en realidad encolaron, la pantalla miente y alguien va a
concluir que el reintento no sirve. Es la misma clase de falla silenciosa que este sub-proyecto
viene a eliminar.

- [ ] **Step 6: Correr los tests**

Expected: **193 OK**. Los tests que asumían 200 en `/sales/` hay que actualizarlos al 202 — **son
del contrato viejo, no regresiones**. Si algún test cubría los botones del admin, sus asserts sobre
el mensaje también cambian.

- [ ] **Step 7: Commit**

```bash
git add core/views.py core/states.py core/tests.py core/admin.py \
        core/management/commands/retryfaileds.py \
        core/management/commands/sync_bims_contacts.py
git commit -m "feat(cola): el ingreso persiste y devuelve 202

Woo deshabilita el webhook a las 5 no-2xx seguidas y hoy devolvemos 503 ante
cualquier excepcion de BIMS. Con 202 el unico no-2xx que queda es el 400 por
request malformado, que no depende de terceros.

Los CUATRO reintentos pasan a escribir en la BD: los dos comandos y los dos del
admin chequeaban status_code == 200, y con el 202 habrian dejado de funcionar en
silencio. Los del admin ademas avisaban 'procesadas correctamente', asi que la
pantalla habria mentido."
```

---

## Task 6: El worker y el reaper

**Files:**
- Create: `core/management/commands/process_queue.py`, `process-queue.sh`
- Test: `core/tests.py`

**Interfaces:**
- Consumes: `enqueue`, los estados, `process_order`.

- [ ] **Step 1: Escribir los tests que fallan**

```python
class WorkerDeColaTest(TestCase):
    @patch("core.management.commands.process_queue.process_order")
    def test_procesa_las_pendientes_y_no_las_demas(self, mock_process):
        FailedOrder.objects.create(external_reference="1", status=FailedOrder.PENDING)
        FailedOrder.objects.create(external_reference="2", status=FailedOrder.COMPLETED)
        call_command("process_queue")
        mock_process.assert_called_once_with(order_id="1")

    @patch("core.management.commands.process_queue.process_order")
    def test_no_toca_filas_con_proximo_intento_en_el_futuro(self, mock_process):
        FailedOrder.objects.create(
            external_reference="1",
            status=FailedOrder.PENDING,
            bims_next_attempt=now() + timedelta(minutes=30),
        )
        call_command("process_queue")
        mock_process.assert_not_called()

    @patch("core.management.commands.process_queue.process_order")
    def test_el_reaper_recupera_una_fila_colgada(self, mock_process):
        """Si un worker muere a mitad, la fila queda PROCESSING para siempre."""
        FailedOrder.objects.create(
            external_reference="1",
            status=FailedOrder.PROCESSING,
            claimed_at=now() - timedelta(minutes=30),
        )
        call_command("process_queue")
        # Reencolada y procesada en la misma corrida.
        mock_process.assert_called_once_with(order_id="1")
```

- [ ] **Step 2: Correr y verificar que fallan**

Expected: FAIL con `CommandError: Unknown command: 'process_queue'`

- [ ] **Step 3: Implementar el comando**

```python
"""
Worker de la cola. Corre por cron cada minuto, envuelto en `flock`.

El reaper corre PRIMERO: si un worker murio a mitad de camino su fila quedo en
PROCESSING para siempre. Es seguro porque **BIMS deduplica por `_id`**, asi que
reprocesar no emite una segunda factura. Sin esa garantia, un reaper sobre datos
fiscales seria inaceptable.
"""
from datetime import timedelta
from typing import List

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import models, transaction
from django.utils.timezone import now

from core.models import FailedOrder
from core.services import process_order
from core.woocommerce import wc_api  # lo usa `_repair_woo_metas` en la Tarea 7

LOTE = 20


class Command(BaseCommand):
    help = "Procesa la cola de transacciones pendientes."

    def handle(self, *args, **options):
        self._reap_stale()
        for referencia in self._tomar():
            try:
                process_order(order_id=referencia)
            except Exception:
                # `process_order` ya deja el FailedOrder en su estado correcto.
                # Tragar aca es deliberado: una orden rota no debe frenar el lote.
                continue

    def _reap_stale(self) -> None:
        limite = now() - timedelta(minutes=settings.QUEUE_REAPER_MINUTES)
        FailedOrder.objects.filter(
            status=FailedOrder.PROCESSING, claimed_at__lt=limite
        ).update(status=FailedOrder.PENDING, claimed_at=None)

    def _tomar(self) -> List[str]:
        """
        `SKIP LOCKED` deja que dos corridas solapadas no se peleen por la misma
        fila. `flock` deberia evitar el solapamiento, pero el cinturon no cuesta.
        """
        with transaction.atomic():
            filas = list(
                FailedOrder.objects.select_for_update(skip_locked=True)
                .filter(status=FailedOrder.PENDING)
                .filter(models.Q(bims_next_attempt__isnull=True)
                        | models.Q(bims_next_attempt__lte=now()))
                .order_by("id")[:LOTE]
            )
            ids = [f.id for f in filas]
            FailedOrder.objects.filter(id__in=ids).update(
                status=FailedOrder.PROCESSING, claimed_at=now()
            )
        return [f.external_reference for f in filas]
```

Agregar a `settings.py`: `QUEUE_REAPER_MINUTES = int(config.get("QUEUE_REAPER_MINUTES", 10))`.

- [ ] **Step 4: Correr los tests**

Expected: **196 OK**.

- [ ] **Step 5: El envoltorio del cron**

`process-queue.sh`:

```bash
#!/usr/bin/env bash
#
# Worker de la cola, para el cron. `flock -n` sale sin hacer nada si ya hay una
# corrida en curso: sin eso, una corrida lenta se solaparia con la siguiente.
set -euo pipefail
exec /usr/bin/flock -n /var/lock/process-queue.lock \
    /root/venv-integrador-52/bin/python /var/www/integrador/manage.py process_queue
```

`chmod +x`. La línea de cron (la instala Carlos en el Despliegue 2):
`* * * * * /var/www/integrador/process-queue.sh >> /var/log/process-queue.log 2>&1`

- [ ] **Step 6: Commit**

```bash
git add core/management/commands/process_queue.py process-queue.sh core/tests.py muci-integrador/settings.py
git commit -m "feat(cola): worker con SKIP LOCKED y reaper de filas colgadas"
```

---

## Task 7: Reintentos por rama

**Files:** Modify `core/management/commands/process_queue.py`, `core/services.py`; Test `core/tests.py`

- [ ] **Step 1: Escribir los tests que fallan**

```python
class ReintentosPorRamaTest(TestCase):
    @patch("core.management.commands.process_queue.process_order", side_effect=ValueError("BIMS caido"))
    def test_un_fallo_agenda_el_proximo_intento_con_backoff(self, _p):
        FailedOrder.objects.create(external_reference="1", status=FailedOrder.PENDING)
        call_command("process_queue")
        f = FailedOrder.objects.get(external_reference="1")
        self.assertEqual(f.bims_attempts, 1)
        self.assertIsNotNone(f.bims_next_attempt)
        self.assertEqual(f.status, FailedOrder.PENDING)

    @patch("core.management.commands.process_queue.process_order", side_effect=ValueError("BIMS caido"))
    def test_agotados_los_intentos_queda_FAILED(self, _p):
        FailedOrder.objects.create(
            external_reference="1", status=FailedOrder.PENDING, bims_attempts=4
        )
        call_command("process_queue")
        self.assertEqual(
            FailedOrder.objects.get(external_reference="1").status, FailedOrder.FAILED
        )

    @patch("core.management.commands.process_queue.wc_api")
    def test_una_venta_facturada_sin_meta_se_repara_en_la_pasada_siguiente(self, mock_wc):
        """El caso 204000: facturo, la meta no quedo, y hoy se perdia."""
        FailedOrder.objects.create(
            external_reference="204000",
            status=FailedOrder.COMPLETED,
            bims_sale_id="31385",
            bims_invoice_number="12040",
            woo_meta_ok=False,
        )
        call_command("process_queue")
        mock_wc.update_order_meta.assert_called_once_with(
            "204000", {"_bims_sale_id": "31385", "_bims_invoice_number": "12040"}
        )
        self.assertTrue(FailedOrder.objects.get(external_reference="204000").woo_meta_ok)
```

- [ ] **Step 2: Correr y verificar que fallan**

- [ ] **Step 3: Implementar el backoff y la reparación de la rama Woo**

```python
# Minutos entre intentos. El primero rapido atrapa el error transitorio; los
# siguientes esperan a que alguien arregle BIMS. Spec §6.4.
BACKOFF_MINUTES = (1, 5, 15, 60)
MAX_BIMS_ATTEMPTS = 5
MAX_META_ATTEMPTS = 20
```

En `handle`, envolver `process_order` y agregar la pasada de reparación:

```python
            except Exception:
                self._schedule_retry(referencia)
                continue

    def _schedule_retry(self, referencia: str) -> None:
        f = FailedOrder.objects.get(
            external_reference=referencia, origin=FailedOrder.ORIGIN_WOO
        )
        f.bims_attempts += 1
        if f.bims_attempts >= MAX_BIMS_ATTEMPTS:
            f.status = FailedOrder.FAILED
        else:
            espera = BACKOFF_MINUTES[min(f.bims_attempts - 1, len(BACKOFF_MINUTES) - 1)]
            f.status = FailedOrder.PENDING
            f.bims_next_attempt = now() + timedelta(minutes=espera)
        f.claimed_at = None
        f.save()

    def _repair_woo_metas(self) -> None:
        """
        La rama de Woo no lleva backoff propio: es barata e idempotente y le
        alcanza con reintentarse en cada pasada. Spec §5.2.
        """
        pendientes = FailedOrder.objects.filter(
            status=FailedOrder.COMPLETED, woo_meta_ok=False, bims_sale_id__isnull=False
        )[:LOTE]
        for f in pendientes:
            meta = {"_bims_sale_id": f.bims_sale_id}
            if f.bims_invoice_number:
                meta["_bims_invoice_number"] = f.bims_invoice_number
            try:
                wc_api.update_order_meta(f.external_reference, meta)
            except Exception:
                continue
            f.woo_meta_ok = True
            f.save(update_fields=["woo_meta_ok"])
```

Llamar a `self._repair_woo_metas()` al final de `handle`.

- [ ] **Step 4: Correr los tests**

Expected: **199 OK**.

- [ ] **Step 5: Commit**

```bash
git add core/management/commands/process_queue.py core/tests.py
git commit -m "feat(cola): backoff para BIMS y auto-reparacion de la meta en Woo"
```

---

## Task 8: Alerta a Slack y corrección del logging

**Files:** Create `core/alerts.py`; Modify `core/management/commands/process_queue.py`,
`core/bims.py`, `muci-integrador/settings.py`, `.env.example`; Test `core/tests.py`

⚠️ **Sin esta tarea, A empeora el sistema**: cambia una falla ruidosa por una invisible (spec §7.3).

- [ ] **Step 1: Escribir los tests que fallan**

```python
class AlertasTest(TestCase):
    @patch("core.alerts.requests.post")
    def test_alerta_cuando_la_cola_pasa_el_umbral(self, mock_post):
        for i in range(15):
            FailedOrder.objects.create(external_reference=str(i), status=FailedOrder.PENDING)
        call_command("process_queue")
        self.assertTrue(mock_post.called)
        self.assertIn("cola", mock_post.call_args[1]["json"]["text"].lower())

    @patch("core.alerts.requests.post")
    def test_el_throttle_evita_la_repeticion(self, mock_post):
        from core.alerts import avisar
        notify("cola_larga", "hola")
        notify("cola_larga", "hola")
        self.assertEqual(mock_post.call_count, 1)

    @patch("core.alerts.requests.post")
    def test_sin_webhook_configurado_no_revienta(self, mock_post):
        with self.settings(SLACK_WEBHOOK_URL=""):
            from core.alerts import avisar
            notify("cola_larga", "hola")
        mock_post.assert_not_called()
```

- [ ] **Step 2: Correr y verificar que fallan**

- [ ] **Step 3: Implementar `core/alerts.py`**

```python
"""
Avisos a Slack. Sentry queda para problemas de codigo; Slack para transacciones
que no llegaron. Fail-safe: un problema al avisar nunca debe romper el flujo.
"""
import logging
from typing import Optional

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

TIMEOUT = 5
THROTTLE_SECONDS = 15 * 60


def notify(clave: str, texto: str) -> None:
    url: Optional[str] = getattr(settings, "SLACK_WEBHOOK_URL", "")
    if not url:
        return
    # Una caida de BIMS no debe mandar 200 mensajes.
    if not cache.add(f"alerta:{clave}", 1, THROTTLE_SECONDS):
        return
    try:
        requests.post(url, json={"text": texto}, timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"No se pudo avisar a Slack ({clave}): {e}")
```

`settings.py`: `SLACK_WEBHOOK_URL = config.get("SLACK_WEBHOOK_URL", "")`,
`QUEUE_ALERT_THRESHOLD = int(config.get("QUEUE_ALERT_THRESHOLD", 10))`,
`QUEUE_SILENCE_MINUTES = int(config.get("QUEUE_SILENCE_MINUTES", 10))`.
Y las tres líneas en `.env.example`.

- [ ] **Step 4: Los tres disparadores en el worker**

Al final de `handle`: cola por encima del umbral, órdenes que agotaron reintentos, y **nada
procesado en X minutos** (el único que detecta que el cron está muerto).

- [ ] **Step 5: Bajar a `warning` los fallos de negocio esperados**

`settings.py` usa `LoggingIntegration(event_level=logging.ERROR)`, así que **cada `logger.error()`
es un evento de Sentry** y `bims.py` loguea un error **por reintento**: una orden lenta genera 3-4
eventos indistinguibles de un bug. Cambiar a `logger.warning` los reintentos transitorios de
`bims.py`, dejando `logger.error` solo para lo que de verdad es un bug.

- [ ] **Step 6: Correr los tests**

Expected: **202 OK**.

- [ ] **Step 7: Commit**

```bash
git add core/alerts.py core/management/commands/process_queue.py core/bims.py \
        muci-integrador/settings.py .env.example core/tests.py
git commit -m "feat(cola): alertas a Slack y fallos de negocio fuera de Sentry"
```

---

## Task 9: 🚀 DESPLIEGUE 2 — el cambio de contrato

- [ ] **Step 1: Verificar sobre los dos stacks**

`./verificar-en-stack-produccion.sh` (asistente) y la corrida con `PYTHON=` (Carlos). **202 OK** en
ambos.

- [ ] **Step 2: Agregar `SLACK_WEBHOOK_URL` al `.env` de producción (Carlos)**

**Antes del deploy.** Sin webhook las alertas se callan y el 202 esconde todo (spec §7.3).

- [ ] **Step 3: Desplegar código y reiniciar (Carlos)**

Igual que el Step 6 de la Tarea 3, sin `migrate` (no hay migraciones nuevas).

- [ ] **Step 4: Instalar el cron (Carlos)**

```
! ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org 'grep -q process-queue /etc/crontab || echo "* * * * * root /var/www/integrador/process-queue.sh >> /var/log/process-queue.log 2>&1" >> /etc/crontab; grep -c process-queue /etc/crontab'
```
Expected: **`1`**. El `grep -q ||` lo hace idempotente: correrlo dos veces no duplica la línea.

- [ ] **Step 5: Probar la alerta a propósito**

Encolar una referencia inexistente y confirmar que llega el mensaje a Slack. **Una alerta que nunca
se probó no es una alerta.**

- [ ] **Step 6: Verificar con una venta real**

`POST /sales/` → **202** en milisegundos; en menos de ~1 minuto la fila pasa a `COMPLETED` con
`bims_sale_id`; y la orden de Woo con `_bims_sale_id`. Confirmar en el log del cron que el worker
corre cada minuto.

- [ ] **Step 7: Vigilar el resto del día**

Que la cola no crezca, que no haya `PROCESSING` colgadas, y que la alerta de tamaño no dispare — o
que dispare y **ahí tengamos por fin el dato del pico** (spec §7.1).

**Rollback:** sacar la línea del cron, `git reset` al commit anterior, `systemctl restart`. **Las
migraciones 0009 y 0010 se quedan** — son compatibles con el código viejo salvo por el nombre del
campo, así que el rollback del Despliegue 2 vuelve al Despliegue 1, no más atrás.

---

## Self-review

**Cobertura de la spec:**

| sección | tarea |
|---|---|
| §4 contrato 202 e idempotencia del ingreso | 5 |
| §5.1 estados | 1, 4 |
| §5.2 campos y asimetría entre ramas | 1, 7 |
| §5.3 migración | 2, 3 |
| §5.4 consumidores | 2, 5 |
| §6.1 worker y reaper | 6 |
| §6.2 alerta | 8 |
| §6.3 corrección del logging | 8 |
| §6.4 parámetros | 6, 7, 8 |
| §7 riesgos | 3, 9 |
| §8 testing | en cada tarea |

**Sin huecos.** Los criterios de éxito §9 se verifican en los pasos 6-7 de la Tarea 9.

**Consistencia de nombres:** `external_reference`, `origin`, `woo_meta_ok`, `bims_attempts`,
`bims_next_attempt`, `claimed_at`, `enqueue()`, `mark_not_applicable()`, `notify()`,
`process_queue`. Usados igual en todas las tareas.

**Conteo de tests esperado:** 183 → 186 (T1) → 188 (T2) → 189 (T4) → 193 (T5) → 196 (T6) → 199 (T7)
→ **202** (T8).

**Riesgo residual conocido:** la Tarea 2 es la única que toca datos fiscales y por eso viaja sola en
el Despliegue 1, con backup inmediatamente antes y conteos antes/después.
