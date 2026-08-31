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
- **Nomenclatura:** el código de dominio está en español (`referencia_externa`, `origen`). Mantenerlo.
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
| `core/migrations/0010_*.py` | identidad + `unique` + migración de datos | 2 |
| `core/estados.py` | **nuevo** — helpers de transición, sin dependencias de red | 4 |
| `core/views.py` | `SalesView`: validar, persistir, `202` | 5 |
| `core/services.py` | `process_order`: marcar `NO_APLICA`/`PAUSADA`; sin cambios de negocio | 4, 7 |
| `core/management/commands/procesar_cola.py` | **nuevo** — worker + reaper | 6 |
| `core/alertas.py` | **nuevo** — cliente de Slack con throttling | 8 |
| `core/admin.py` | colores y filtros de los estados nuevos | 1, 2 |
| `core/management/commands/retryfaileds.py` | reencolar por BD, no por HTTP | 5 |
| `core/management/commands/sync_bims_contacts.py` | leer `PAUSADA`, reencolar por BD | 4, 5 |
| `procesar-cola.sh` | **nuevo** — envoltorio con `flock` para el cron | 6 |

**Decisión de secuencia:** todo el trabajo de **esquema** (Tareas 1-2) va primero y se despliega
**sin cambiar comportamiento** (Tarea 3). Recién después va el cambio de contrato. Así el despliegue
más riesgoso —la migración de datos sobre la tabla fiscal— viaja solo, y si algo sale mal se sabe
exactamente qué lo causó.

---

### ⚠️ Hallazgo previo al plan: dos consumidores se rompen con el 202

`retryfaileds.py:28` y `sync_bims_contacts.py:78` hacen `POST` a `/sales/` y chequean
**`response.status_code == 200`**. Con el `202` esa condición **nunca vuelve a ser cierta**, y los
dos reintentos dejarían de funcionar **en silencio**.

Además, con la cola, reencolar por HTTP contra nosotros mismos deja de tener sentido: es escribir
`PENDIENTE` en una fila. **La Tarea 5 los convierte a escritura directa en BD.** No es opcional.

---

## Task 1: Estados y campos nuevos (aditivo, sin cambiar comportamiento)

**Files:**
- Modify: `core/models.py` (clase `FailedOrder`)
- Create: `core/migrations/0009_estados_y_campos_de_cola.py` (generada)
- Modify: `core/admin.py:39-48` (`colored_status`), `:30` (`list_filter`)
- Test: `core/tests.py`

**Interfaces:**
- Produces: `FailedOrder.PENDIENTE=3`, `EN_PROCESO=4`, `PAUSADA=5`, `NO_APLICA=6`; campos
  `origen`, `intentos_bims`, `proximo_intento_bims`, `meta_woo_ok`, `tomada_en`.

- [ ] **Step 1: Escribir el test que falla**

```python
class EstadosDeColaTest(TestCase):
    def test_los_estados_existentes_conservan_su_valor(self):
        """8588 filas en produccion dependen de estos dos numeros."""
        self.assertEqual(FailedOrder.FAILED, 1)
        self.assertEqual(FailedOrder.COMPLETED, 2)

    def test_los_estados_nuevos_existen_con_sus_valores(self):
        self.assertEqual(FailedOrder.PENDIENTE, 3)
        self.assertEqual(FailedOrder.EN_PROCESO, 4)
        self.assertEqual(FailedOrder.PAUSADA, 5)
        self.assertEqual(FailedOrder.NO_APLICA, 6)

    def test_los_campos_de_cola_tienen_defaults_seguros(self):
        f = FailedOrder.objects.create(order_id=1)
        self.assertEqual(f.origen, "woo")
        self.assertEqual(f.intentos_bims, 0)
        self.assertIsNone(f.proximo_intento_bims)
        self.assertFalse(f.meta_woo_ok)
        self.assertIsNone(f.tomada_en)
```

- [ ] **Step 2: Correr y verificar que falla**

Run: `.venv/bin/python manage.py test core.tests.EstadosDeColaTest --settings=muci-integrador.test_settings`
Expected: FAIL con `AttributeError: type object 'FailedOrder' has no attribute 'PENDIENTE'`

- [ ] **Step 3: Implementar en el modelo**

En `core/models.py`, dentro de `FailedOrder`, reemplazar el bloque de estados:

```python
    FAILED = 1
    COMPLETED = 2
    # Los dos de arriba YA EXISTEN en produccion con esos valores: 8588 filas
    # dependen de ellos. Los nuevos se agregan arriba, nunca renumerando.
    PENDIENTE = 3
    EN_PROCESO = 4
    PAUSADA = 5
    NO_APLICA = 6
    STATUS_CHOICES = (
        (FAILED, "Fallido"),
        (COMPLETED, "Completado"),
        (PENDIENTE, "Pendiente"),
        (EN_PROCESO, "En proceso"),
        (PAUSADA, "Pausada"),
        (NO_APLICA, "No aplica"),
    )

    ORIGEN_WOO = "woo"
    ORIGEN_CRM = "crm"  # sin uso en el sub-proyecto A; lo estrena F
    ORIGEN_CHOICES = ((ORIGEN_WOO, "WooCommerce"), (ORIGEN_CRM, "CRM Krayin"))
```

Y los campos nuevos, después de `bims_invoice_number`:

```python
    origen = models.CharField(
        verbose_name="Origen",
        max_length=8,
        choices=ORIGEN_CHOICES,
        default=ORIGEN_WOO,
        db_index=True,
    )
    intentos_bims = models.PositiveSmallIntegerField(
        verbose_name="Intentos contra BIMS", default=0
    )
    proximo_intento_bims = models.DateTimeField(
        verbose_name="Próximo intento", null=True, blank=True, db_index=True
    )
    # La rama de anotar en WooCommerce no lleva backoff propio: es una llamada
    # barata e idempotente, y le alcanza con reintentarse en cada pasada. Ver
    # §5.2 de la spec.
    meta_woo_ok = models.BooleanField(verbose_name="Anotada en WooCommerce", default=False)
    tomada_en = models.DateTimeField(
        verbose_name="Tomada por el worker", null=True, blank=True
    )
```

- [ ] **Step 4: Generar la migración y correr los tests**

```bash
.venv/bin/python manage.py makemigrations core --settings=muci-integrador.test_settings
.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings
```
Expected: migración `0009_*` creada; **183 tests + 3 nuevos = 186, OK**.

- [ ] **Step 5: Actualizar el admin para que los estados nuevos se vean**

En `core/admin.py`, reemplazar el diccionario de colores hardcodeado:

```python
    def colored_status(self, obj):
        colors = {
            FailedOrder.FAILED: "red",
            FailedOrder.COMPLETED: "green",
            FailedOrder.PENDIENTE: "#F37043",
            FailedOrder.EN_PROCESO: "#6950A1",
            FailedOrder.PAUSADA: "#F17DB1",
            FailedOrder.NO_APLICA: "gray",
        }
```

Y agregar `origen` al filtro: `list_filter = ("status", "origen")`.

- [ ] **Step 6: Commit**

```bash
git add core/models.py core/admin.py core/tests.py core/migrations/0009_estados_y_campos_de_cola.py
git commit -m "feat(cola): estados y campos de cola en FailedOrder

Aditivo: FAILED=1 y COMPLETED=2 conservan su valor porque 8588 filas de
produccion dependen de ellos. Sin cambios de comportamiento todavia."
```

---

## Task 2: Identidad generalizada (`referencia_externa` + `unique` compuesto)

⚠️ **La tarea más riesgosa del plan: es la única que toca datos de la tabla de estado fiscal.**

**Files:**
- Modify: `core/models.py`, `core/admin.py`, `core/services.py`,
  `core/management/commands/retryfaileds.py`, `core/management/commands/sync_bims_contacts.py`
- Create: `core/migrations/0010_identidad_por_origen.py` (a mano, no autogenerada)
- Test: `core/tests.py` (**23 referencias a `order_id` a actualizar**)

**Interfaces:**
- Consumes: los estados de la Tarea 1.
- Produces: `FailedOrder.referencia_externa: str`, `unique_together = ("origen", "referencia_externa")`.

- [ ] **Step 1: Escribir el test que falla**

```python
class IdentidadPorOrigenTest(TestCase):
    def test_la_misma_referencia_en_origenes_distintos_convive(self):
        FailedOrder.objects.create(referencia_externa="204000", origen=FailedOrder.ORIGEN_WOO)
        FailedOrder.objects.create(referencia_externa="204000", origen=FailedOrder.ORIGEN_CRM)
        self.assertEqual(FailedOrder.objects.count(), 2)

    def test_la_misma_referencia_en_el_mismo_origen_no_se_duplica(self):
        FailedOrder.objects.create(referencia_externa="204000", origen=FailedOrder.ORIGEN_WOO)
        with self.assertRaises(IntegrityError):
            FailedOrder.objects.create(
                referencia_externa="204000", origen=FailedOrder.ORIGEN_WOO
            )
```

- [ ] **Step 2: Correr y verificar que falla**

Run: `.venv/bin/python manage.py test core.tests.IdentidadPorOrigenTest --settings=muci-integrador.test_settings`
Expected: FAIL con `TypeError: FailedOrder() got unexpected keyword arguments: 'referencia_externa'`

- [ ] **Step 3: Cambiar el modelo**

En `core/models.py`, reemplazar el campo `order_id`:

```python
    # Texto, no entero: el CRM no usa ids numericos. El `unique` es compuesto
    # con `origen` porque una referencia solo es unica dentro de su sistema.
    referencia_externa = models.CharField(
        verbose_name="Referencia externa", max_length=64, db_index=True
    )
```

Y en `class Meta`, agregar:

```python
        unique_together = ("origen", "referencia_externa")
```

- [ ] **Step 4: Escribir la migración a mano**

`makemigrations` propondría *borrar* `order_id` y *crear* `referencia_externa`, lo que **perdería
los 8588 valores**. Hay que usar `RenameField` seguido de `AlterField`:

```python
from django.db import migrations, models
import datetime


def marcar_meta_woo(apps, schema_editor):
    """
    Las COMPLETED anteriores al 2026-08-28 no tienen meta porque la feature no
    existia: marcarlas en falso dispararia ~8000 reintentos inutiles contra Woo.
    Las del 28/08 en adelante quedan en False a proposito: de esas no sabemos si
    la meta llego, son una decena, y el worker las repara en la primera pasada.
    La orden 204000 es una de ellas.
    """
    FailedOrder = apps.get_model("core", "FailedOrder")
    corte = datetime.datetime(2026, 8, 28, tzinfo=datetime.timezone.utc)
    FailedOrder.objects.filter(status=2, created_at__lt=corte).update(meta_woo_ok=True)


def desmarcar_meta_woo(apps, schema_editor):
    apps.get_model("core", "FailedOrder").objects.update(meta_woo_ok=False)


def pausadas_a_estado(apps, schema_editor):
    """El canal de estado improvisado pasa a ser un estado real."""
    FailedOrder = apps.get_model("core", "FailedOrder")
    FailedOrder.objects.filter(
        status=1, message__startswith="Pausada: Esperando"
    ).update(status=5)


def pausadas_a_mensaje(apps, schema_editor):
    apps.get_model("core", "FailedOrder").objects.filter(status=5).update(status=1)


class Migration(migrations.Migration):
    dependencies = [("core", "0009_estados_y_campos_de_cola")]
    operations = [
        migrations.RenameField("failedorder", "order_id", "referencia_externa"),
        migrations.AlterField(
            "failedorder",
            "referencia_externa",
            models.CharField(max_length=64, db_index=True, verbose_name="Referencia externa"),
        ),
        migrations.AlterUniqueTogether("failedorder", {("origen", "referencia_externa")}),
        migrations.RunPython(marcar_meta_woo, desmarcar_meta_woo),
        migrations.RunPython(pausadas_a_estado, pausadas_a_mensaje),
    ]
```

- [ ] **Step 5: Actualizar los cinco consumidores**

| archivo | cambio |
|---|---|
| `core/services.py` | todo `order_id=order_id` en `update_or_create` → `referencia_externa=str(order_id), origen=FailedOrder.ORIGEN_WOO` |
| `core/admin.py:21,27,28,29` | `order_id` → `referencia_externa` en `list_display`, `list_display_links`, `search_fields`, `ordering` |
| `retryfaileds.py:25,44` | `order.order_id` → `order.referencia_externa` |
| `sync_bims_contacts.py:63,71,73` | `message__startswith(...)` → `status=FailedOrder.PAUSADA`; `order.order_id` → `order.referencia_externa` |
| `core/tests.py` | las 23 referencias |

- [ ] **Step 6: Correr toda la suite**

Run: `.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings`
Expected: **188 OK**. Si algo falla, es un consumidor sin migrar: arreglarlo, no ajustar el test.

- [ ] **Step 7: Probar la migración contra una copia de los datos reales**

**No** contra producción. Restaurar el último dump en una base local y correr `migrate`, después
verificar los conteos:

```bash
.venv/bin/python manage.py migrate core --settings=<settings-de-la-copia>
# Verificar: total de filas identico; 0 con referencia_externa vacia;
# COMPLETED anteriores al 28/08 con meta_woo_ok=True; las "Pausada: Esperando" en estado 5.
```

- [ ] **Step 8: Commit**

```bash
git add core/models.py core/admin.py core/services.py core/tests.py \
        core/management/commands/retryfaileds.py \
        core/management/commands/sync_bims_contacts.py \
        core/migrations/0010_identidad_por_origen.py
git commit -m "feat(cola): identidad por (origen, referencia_externa)

RenameField + AlterField, no drop/create: borrar y recrear perderia los 8588
valores. Migracion de datos reversible para meta_woo_ok y para las pausadas,
que dejan de vivir en un prefijo de texto."
```

---

## Task 3: 🚀 DESPLIEGUE 1 — solo esquema, comportamiento idéntico

**Files:** ninguno. Es un despliegue.

**Por qué va solo:** es la única migración de datos sobre la tabla fiscal. Viajando sola, si algo
sale mal se sabe exactamente qué lo causó, y el rollback es una sola migración hacia atrás.

- [ ] **Step 1: Verificar sobre el stack de rollback (lo corre el asistente)**

```bash
./verificar-en-stack-produccion.sh
```
Expected: `VERDE`, 188 tests.

- [ ] **Step 2: Verificar sobre el stack REAL (lo corre Carlos)**

```
! PYTHON=/root/venv-integrador-52/bin/python SERVIDOR=root@muci.org REMOTO=wt-verificacion-52 ./verificar-en-stack-produccion.sh
```
Expected: `Python 3.10.12 | Django 5.2.17`, 188 OK.

- [ ] **Step 3: Backup inmediatamente antes (lo corre Carlos)**

```
! ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org 'cd /var/www/integrador && MYSQL_PWD=<pass> ./backup-bases.sh pre-migracion-0010'
```
Expected: las 4 bases, con `Dump completed on`. **Sin esto no se sigue.**

- [ ] **Step 4: Ver qué va a hacer la migración, sin aplicarla (lo corre Carlos)**

```
! ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org 'cd /var/www/integrador && /root/venv-integrador-52/bin/python manage.py migrate core --plan'
```
Expected: exactamente `0009` y `0010` sin aplicar, nada más.

- [ ] **Step 5: Conteos ANTES (lo corre Carlos)**

```
! ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org 'cd /var/www/integrador && /root/venv-integrador-52/bin/python manage.py shell -c "
import sentry_sdk; sentry_sdk.get_global_scope().set_client(None)
from core.models import FailedOrder
from django.db.models import Count
print(list(FailedOrder.objects.values(\"status\").annotate(n=Count(\"id\")).order_by(\"status\")))
print(\"total:\", FailedOrder.objects.count())
"'
```
Anotar la salida. Esperado hoy: `{1: 201, 2: 8387}`, total 8588 (los números pueden haber
crecido; lo que importa es comparar antes/después).

- [ ] **Step 6: Desplegar (lo corre Carlos)**

```
! ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org 'cd /var/www/integrador && git pull --ff-only 2>&1 | tail -3 && git log --oneline -1 && /root/venv-integrador-52/bin/python manage.py migrate core 2>&1 | tail -5 && systemctl restart mucintegrador.service && sleep 5 && systemctl is-active mucintegrador.service'
```
Expected: `Applying core.0009… OK`, `Applying core.0010… OK`, `active`.

- [ ] **Step 7: Conteos DESPUÉS y verificación**

Repetir el Step 5. **El total tiene que ser idéntico.** Además: 0 filas con
`referencia_externa` vacía, y las que tenían `"Pausada: Esperando"` ahora en estado 5.

- [ ] **Step 8: Confirmar que el comportamiento NO cambió**

Esperar una venta real. Tiene que facturar exactamente como antes: `POST /sales/` → **200**,
`FailedOrder` en `COMPLETED` con `bims_sale_id`, y metas en la orden de Woo.

**Rollback si algo falla:** `migrate core 0008` (las dos migraciones son reversibles), `git reset`
al commit anterior, `systemctl restart`. Si la reversión de datos fallara, restaurar del backup del
Step 3.

---

## Task 4: Los estados nuevos se usan de verdad (todavía sin async)

**Files:**
- Create: `core/estados.py`
- Modify: `core/services.py` (las tres ramas de retorno temprano)
- Test: `core/tests.py`

**Interfaces:**
- Produces: `core.estados.marcar_no_aplica(referencia: str, motivo: str) -> None`

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

        f = FailedOrder.objects.get(referencia_externa="202707")
        self.assertEqual(f.status, FailedOrder.NO_APLICA)
        self.assertIn("Monto 0", f.message)
        mock_bims.create_sale.assert_not_called()
```

- [ ] **Step 2: Correr y verificar que falla**

Expected: FAIL con `FailedOrder.DoesNotExist` — que es exactamente el bug.

- [ ] **Step 3: Crear el helper**

`core/estados.py`:

```python
"""
Transiciones de estado de `FailedOrder`, sin dependencias de red.

Vive aparte de `services.py` a proposito: `services` importa `core.bims`, que
instancia `BimsApi()` en el import y **dispara un login real contra BIMS**. Un
helper de estado no tiene por que arrastrar eso.
"""
from typing import Optional

from core.models import FailedOrder


def marcar_no_aplica(referencia: str, motivo: str) -> None:
    """La transaccion no corresponde facturar. Estado terminal, sin reintento."""
    FailedOrder.objects.update_or_create(
        referencia_externa=str(referencia),
        origen=FailedOrder.ORIGEN_WOO,
        defaults={"status": FailedOrder.NO_APLICA, "message": motivo},
    )
```

- [ ] **Step 4: Usarlo en las tres ramas de retorno temprano de `services.py`**

Reemplazar cada `return {"status": ...}` temprano por una llamada previa:

```python
        marcar_no_aplica(order_id, "Descuento 100%" if discount > 0 else "Monto 0")
        return {"status": "Descuento 100%" if discount > 0 else "Monto 0"}
```

Idem para `"No procesado"` y `"Productos en 0"`.

- [ ] **Step 5: Correr los tests**

Expected: **189 OK**.

- [ ] **Step 6: Commit**

```bash
git add core/estados.py core/services.py core/tests.py
git commit -m "feat(cola): las ordenes que no corresponde facturar dejan fila NO_APLICA"
```

---

## Task 5: El ingreso devuelve 202 y persiste

⚠️ **Cambia el contrato con WooCommerce.** No desplegar sin la Tarea 6: sin worker, nada se procesa.

**Files:**
- Modify: `core/views.py` (`SalesView.post`), `core/estados.py`
- Modify: `core/management/commands/retryfaileds.py`, `core/management/commands/sync_bims_contacts.py`
- Test: `core/tests.py`

**Interfaces:**
- Produces: `core.estados.encolar(referencia: str, origen: str = "woo") -> FailedOrder`

- [ ] **Step 1: Escribir los tests que fallan**

```python
class IngresoAsincronoTest(TestCase):
    @patch("core.views.process_order")
    def test_el_ingreso_responde_202_y_no_procesa_nada(self, mock_process):
        r = self.client.post("/sales/", {"arg": 204000}, format="json")
        self.assertEqual(r.status_code, 202)
        mock_process.assert_not_called()
        self.assertEqual(
            FailedOrder.objects.get(referencia_externa="204000").status,
            FailedOrder.PENDIENTE,
        )

    def test_sin_referencia_sigue_siendo_400(self):
        self.assertEqual(self.client.post("/sales/", {}, format="json").status_code, 400)

    def test_una_reentrega_de_orden_completada_no_la_reencola(self):
        """Ya se facturo: reprocesar es riesgo sin beneficio. Spec §4."""
        FailedOrder.objects.create(
            referencia_externa="204000", status=FailedOrder.COMPLETED, bims_sale_id="31385"
        )
        self.client.post("/sales/", {"arg": 204000}, format="json")
        self.assertEqual(
            FailedOrder.objects.get(referencia_externa="204000").status,
            FailedOrder.COMPLETED,
        )

    def test_una_reentrega_de_orden_fallida_la_reencola(self):
        FailedOrder.objects.create(referencia_externa="204000", status=FailedOrder.FAILED)
        self.client.post("/sales/", {"arg": 204000}, format="json")
        self.assertEqual(
            FailedOrder.objects.get(referencia_externa="204000").status,
            FailedOrder.PENDIENTE,
        )
```

- [ ] **Step 2: Correr y verificar que fallan**

Expected: FAIL — el primero con `202 != 200`.

- [ ] **Step 3: Implementar `encolar`**

En `core/estados.py`:

```python
# Estados desde los que una re-entrega vuelve a encolar. COMPLETED queda afuera
# (ya se facturo) y PENDIENTE/EN_PROCESO tambien (ya esta en la cola). Spec §4.
REENCOLABLES = (FailedOrder.FAILED, FailedOrder.NO_APLICA)


def encolar(referencia: str, origen: str = FailedOrder.ORIGEN_WOO) -> FailedOrder:
    fila, creada = FailedOrder.objects.get_or_create(
        referencia_externa=str(referencia),
        origen=origen,
        defaults={"status": FailedOrder.PENDIENTE, "message": "Encolada."},
    )
    if not creada and fila.status in REENCOLABLES:
        fila.status = FailedOrder.PENDIENTE
        fila.message = "Reencolada."
        fila.intentos_bims = 0
        fila.proximo_intento_bims = None
        fila.save(update_fields=["status", "message", "intentos_bims", "proximo_intento_bims"])
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
        # Persistir y salir. El trabajo lo hace `procesar_cola`.
        # Devolver 202 SIEMPRE es deliberado: Woo deshabilita el webhook a las 5
        # respuestas no-2xx seguidas, y ya mato asi al webhook `Refund order`.
        encolar(order_id)
        return Response(data={"status": "encolada"}, status=status.HTTP_202_ACCEPTED)
```

- [ ] **Step 5: Convertir los dos reintentos a escritura directa en BD**

**Crítico:** los dos chequean `status_code == 200`, que con el `202` nunca vuelve a ser cierto.

En `retryfaileds.py`, reemplazar todo el bucle HTTP por:

```python
        from core.estados import encolar

        reencoladas = 0
        for orden in FailedOrder.objects.filter(status=FailedOrder.FAILED):
            encolar(orden.referencia_externa, orden.origen)
            reencoladas += 1
        self.stdout.write(self.style.SUCCESS(f"Reencoladas: {reencoladas}"))
```

En `sync_bims_contacts.py`, reemplazar el bucle de pausadas por el mismo patrón, filtrando
`status=FailedOrder.PAUSADA`. Se van los `import requests` que quedan sin uso en ese bloque.

- [ ] **Step 6: Correr los tests**

Expected: **193 OK**. Los tests que asumían 200 en `/sales/` hay que actualizarlos al 202 — **son
del contrato viejo, no regresiones**.

- [ ] **Step 7: Commit**

```bash
git add core/views.py core/estados.py core/tests.py \
        core/management/commands/retryfaileds.py \
        core/management/commands/sync_bims_contacts.py
git commit -m "feat(cola): el ingreso persiste y devuelve 202

Woo deshabilita el webhook a las 5 no-2xx seguidas y hoy devolvemos 503 ante
cualquier excepcion de BIMS. Con 202 el unico no-2xx que queda es el 400 por
request malformado, que no depende de terceros.

Los dos reintentos pasan a escribir en la BD: chequeaban status_code == 200 y
con el 202 habrian dejado de funcionar en silencio."
```

---

## Task 6: El worker y el reaper

**Files:**
- Create: `core/management/commands/procesar_cola.py`, `procesar-cola.sh`
- Test: `core/tests.py`

**Interfaces:**
- Consumes: `encolar`, los estados, `process_order`.

- [ ] **Step 1: Escribir los tests que fallan**

```python
class WorkerDeColaTest(TestCase):
    @patch("core.management.commands.procesar_cola.process_order")
    def test_procesa_las_pendientes_y_no_las_demas(self, mock_process):
        FailedOrder.objects.create(referencia_externa="1", status=FailedOrder.PENDIENTE)
        FailedOrder.objects.create(referencia_externa="2", status=FailedOrder.COMPLETED)
        call_command("procesar_cola")
        mock_process.assert_called_once_with(order_id="1")

    @patch("core.management.commands.procesar_cola.process_order")
    def test_no_toca_filas_con_proximo_intento_en_el_futuro(self, mock_process):
        FailedOrder.objects.create(
            referencia_externa="1",
            status=FailedOrder.PENDIENTE,
            proximo_intento_bims=now() + timedelta(minutes=30),
        )
        call_command("procesar_cola")
        mock_process.assert_not_called()

    @patch("core.management.commands.procesar_cola.process_order")
    def test_el_reaper_recupera_una_fila_colgada(self, mock_process):
        """Si un worker muere a mitad, la fila queda EN_PROCESO para siempre."""
        FailedOrder.objects.create(
            referencia_externa="1",
            status=FailedOrder.EN_PROCESO,
            tomada_en=now() - timedelta(minutes=30),
        )
        call_command("procesar_cola")
        # Reencolada y procesada en la misma corrida.
        mock_process.assert_called_once_with(order_id="1")
```

- [ ] **Step 2: Correr y verificar que fallan**

Expected: FAIL con `CommandError: Unknown command: 'procesar_cola'`

- [ ] **Step 3: Implementar el comando**

```python
"""
Worker de la cola. Corre por cron cada minuto, envuelto en `flock`.

El reaper corre PRIMERO: si un worker murio a mitad de camino su fila quedo en
EN_PROCESO para siempre. Es seguro porque **BIMS deduplica por `_id`**, asi que
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
from core.woocommerce import wc_api  # lo usa `_reparar_metas` en la Tarea 7

LOTE = 20


class Command(BaseCommand):
    help = "Procesa la cola de transacciones pendientes."

    def handle(self, *args, **options):
        self._recuperar_colgadas()
        for referencia in self._tomar():
            try:
                process_order(order_id=referencia)
            except Exception:
                # `process_order` ya deja el FailedOrder en su estado correcto.
                # Tragar aca es deliberado: una orden rota no debe frenar el lote.
                continue

    def _recuperar_colgadas(self) -> None:
        limite = now() - timedelta(minutes=settings.COLA_REAPER_MINUTOS)
        FailedOrder.objects.filter(
            status=FailedOrder.EN_PROCESO, tomada_en__lt=limite
        ).update(status=FailedOrder.PENDIENTE, tomada_en=None)

    def _tomar(self) -> List[str]:
        """
        `SKIP LOCKED` deja que dos corridas solapadas no se peleen por la misma
        fila. `flock` deberia evitar el solapamiento, pero el cinturon no cuesta.
        """
        with transaction.atomic():
            filas = list(
                FailedOrder.objects.select_for_update(skip_locked=True)
                .filter(status=FailedOrder.PENDIENTE)
                .filter(models.Q(proximo_intento_bims__isnull=True)
                        | models.Q(proximo_intento_bims__lte=now()))
                .order_by("id")[:LOTE]
            )
            ids = [f.id for f in filas]
            FailedOrder.objects.filter(id__in=ids).update(
                status=FailedOrder.EN_PROCESO, tomada_en=now()
            )
        return [f.referencia_externa for f in filas]
```

Agregar a `settings.py`: `COLA_REAPER_MINUTOS = int(config.get("COLA_REAPER_MINUTOS", 10))`.

- [ ] **Step 4: Correr los tests**

Expected: **196 OK**.

- [ ] **Step 5: El envoltorio del cron**

`procesar-cola.sh`:

```bash
#!/usr/bin/env bash
#
# Worker de la cola, para el cron. `flock -n` sale sin hacer nada si ya hay una
# corrida en curso: sin eso, una corrida lenta se solaparia con la siguiente.
set -euo pipefail
exec /usr/bin/flock -n /var/lock/procesar-cola.lock \
    /root/venv-integrador-52/bin/python /var/www/integrador/manage.py procesar_cola
```

`chmod +x`. La línea de cron (la instala Carlos en el Despliegue 2):
`* * * * * /var/www/integrador/procesar-cola.sh >> /var/log/procesar-cola.log 2>&1`

- [ ] **Step 6: Commit**

```bash
git add core/management/commands/procesar_cola.py procesar-cola.sh core/tests.py muci-integrador/settings.py
git commit -m "feat(cola): worker con SKIP LOCKED y reaper de filas colgadas"
```

---

## Task 7: Reintentos por rama

**Files:** Modify `core/management/commands/procesar_cola.py`, `core/services.py`; Test `core/tests.py`

- [ ] **Step 1: Escribir los tests que fallan**

```python
class ReintentosPorRamaTest(TestCase):
    @patch("core.management.commands.procesar_cola.process_order", side_effect=ValueError("BIMS caido"))
    def test_un_fallo_agenda_el_proximo_intento_con_backoff(self, _p):
        FailedOrder.objects.create(referencia_externa="1", status=FailedOrder.PENDIENTE)
        call_command("procesar_cola")
        f = FailedOrder.objects.get(referencia_externa="1")
        self.assertEqual(f.intentos_bims, 1)
        self.assertIsNotNone(f.proximo_intento_bims)
        self.assertEqual(f.status, FailedOrder.PENDIENTE)

    @patch("core.management.commands.procesar_cola.process_order", side_effect=ValueError("BIMS caido"))
    def test_agotados_los_intentos_queda_FAILED(self, _p):
        FailedOrder.objects.create(
            referencia_externa="1", status=FailedOrder.PENDIENTE, intentos_bims=4
        )
        call_command("procesar_cola")
        self.assertEqual(
            FailedOrder.objects.get(referencia_externa="1").status, FailedOrder.FAILED
        )

    @patch("core.management.commands.procesar_cola.wc_api")
    def test_una_venta_facturada_sin_meta_se_repara_en_la_pasada_siguiente(self, mock_wc):
        """El caso 204000: facturo, la meta no quedo, y hoy se perdia."""
        FailedOrder.objects.create(
            referencia_externa="204000",
            status=FailedOrder.COMPLETED,
            bims_sale_id="31385",
            bims_invoice_number="12040",
            meta_woo_ok=False,
        )
        call_command("procesar_cola")
        mock_wc.update_order_meta.assert_called_once_with(
            "204000", {"_bims_sale_id": "31385", "_bims_invoice_number": "12040"}
        )
        self.assertTrue(FailedOrder.objects.get(referencia_externa="204000").meta_woo_ok)
```

- [ ] **Step 2: Correr y verificar que fallan**

- [ ] **Step 3: Implementar el backoff y la reparación de la rama Woo**

```python
# Minutos entre intentos. El primero rapido atrapa el error transitorio; los
# siguientes esperan a que alguien arregle BIMS. Spec §6.4.
BACKOFF_MINUTOS = (1, 5, 15, 60)
MAX_INTENTOS_BIMS = 5
MAX_INTENTOS_META = 20
```

En `handle`, envolver `process_order` y agregar la pasada de reparación:

```python
            except Exception:
                self._agendar_reintento(referencia)
                continue

    def _agendar_reintento(self, referencia: str) -> None:
        f = FailedOrder.objects.get(
            referencia_externa=referencia, origen=FailedOrder.ORIGEN_WOO
        )
        f.intentos_bims += 1
        if f.intentos_bims >= MAX_INTENTOS_BIMS:
            f.status = FailedOrder.FAILED
        else:
            espera = BACKOFF_MINUTOS[min(f.intentos_bims - 1, len(BACKOFF_MINUTOS) - 1)]
            f.status = FailedOrder.PENDIENTE
            f.proximo_intento_bims = now() + timedelta(minutes=espera)
        f.tomada_en = None
        f.save()

    def _reparar_metas(self) -> None:
        """
        La rama de Woo no lleva backoff propio: es barata e idempotente y le
        alcanza con reintentarse en cada pasada. Spec §5.2.
        """
        pendientes = FailedOrder.objects.filter(
            status=FailedOrder.COMPLETED, meta_woo_ok=False, bims_sale_id__isnull=False
        )[:LOTE]
        for f in pendientes:
            meta = {"_bims_sale_id": f.bims_sale_id}
            if f.bims_invoice_number:
                meta["_bims_invoice_number"] = f.bims_invoice_number
            try:
                wc_api.update_order_meta(f.referencia_externa, meta)
            except Exception:
                continue
            f.meta_woo_ok = True
            f.save(update_fields=["meta_woo_ok"])
```

Llamar a `self._reparar_metas()` al final de `handle`.

- [ ] **Step 4: Correr los tests**

Expected: **199 OK**.

- [ ] **Step 5: Commit**

```bash
git add core/management/commands/procesar_cola.py core/tests.py
git commit -m "feat(cola): backoff para BIMS y auto-reparacion de la meta en Woo"
```

---

## Task 8: Alerta a Slack y corrección del logging

**Files:** Create `core/alertas.py`; Modify `core/management/commands/procesar_cola.py`,
`core/bims.py`, `muci-integrador/settings.py`, `.env.example`; Test `core/tests.py`

⚠️ **Sin esta tarea, A empeora el sistema**: cambia una falla ruidosa por una invisible (spec §7.3).

- [ ] **Step 1: Escribir los tests que fallan**

```python
class AlertasTest(TestCase):
    @patch("core.alertas.requests.post")
    def test_alerta_cuando_la_cola_pasa_el_umbral(self, mock_post):
        for i in range(15):
            FailedOrder.objects.create(referencia_externa=str(i), status=FailedOrder.PENDIENTE)
        call_command("procesar_cola")
        self.assertTrue(mock_post.called)
        self.assertIn("cola", mock_post.call_args[1]["json"]["text"].lower())

    @patch("core.alertas.requests.post")
    def test_el_throttle_evita_la_repeticion(self, mock_post):
        from core.alertas import avisar
        avisar("cola_larga", "hola")
        avisar("cola_larga", "hola")
        self.assertEqual(mock_post.call_count, 1)

    @patch("core.alertas.requests.post")
    def test_sin_webhook_configurado_no_revienta(self, mock_post):
        with self.settings(SLACK_WEBHOOK_URL=""):
            from core.alertas import avisar
            avisar("cola_larga", "hola")
        mock_post.assert_not_called()
```

- [ ] **Step 2: Correr y verificar que fallan**

- [ ] **Step 3: Implementar `core/alertas.py`**

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
THROTTLE_SEGUNDOS = 15 * 60


def avisar(clave: str, texto: str) -> None:
    url: Optional[str] = getattr(settings, "SLACK_WEBHOOK_URL", "")
    if not url:
        return
    # Una caida de BIMS no debe mandar 200 mensajes.
    if not cache.add(f"alerta:{clave}", 1, THROTTLE_SEGUNDOS):
        return
    try:
        requests.post(url, json={"text": texto}, timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"No se pudo avisar a Slack ({clave}): {e}")
```

`settings.py`: `SLACK_WEBHOOK_URL = config.get("SLACK_WEBHOOK_URL", "")`,
`COLA_UMBRAL_ALERTA = int(config.get("COLA_UMBRAL_ALERTA", 10))`,
`COLA_SILENCIO_MINUTOS = int(config.get("COLA_SILENCIO_MINUTOS", 10))`.
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
git add core/alertas.py core/management/commands/procesar_cola.py core/bims.py \
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
! ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org 'grep -q procesar-cola /etc/crontab || echo "* * * * * root /var/www/integrador/procesar-cola.sh >> /var/log/procesar-cola.log 2>&1" >> /etc/crontab; grep -c procesar-cola /etc/crontab'
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

Que la cola no crezca, que no haya `EN_PROCESO` colgadas, y que la alerta de tamaño no dispare — o
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

**Consistencia de nombres:** `referencia_externa`, `origen`, `meta_woo_ok`, `intentos_bims`,
`proximo_intento_bims`, `tomada_en`, `encolar()`, `marcar_no_aplica()`, `avisar()`,
`procesar_cola`. Usados igual en todas las tareas.

**Conteo de tests esperado:** 183 → 186 (T1) → 188 (T2) → 189 (T4) → 193 (T5) → 196 (T6) → 199 (T7)
→ **202** (T8).

**Riesgo residual conocido:** la Tarea 2 es la única que toca datos fiscales y por eso viaja sola en
el Despliegue 1, con backup inmediatamente antes y conteos antes/después.
