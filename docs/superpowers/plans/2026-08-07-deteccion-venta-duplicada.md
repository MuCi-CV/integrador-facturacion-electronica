# Detección de facturas duplicadas — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detectar y alertar si BIMS alguna vez deja de deduplicar ventas por `_id` y un mismo pedido de WooCommerce genera dos facturas.

**Architecture:** `create_sale` pasa a informar si BIMS devolvió una venta preexistente en lugar de crear una nueva. El `bims_sale_id` se persiste por orden en `FailedOrder`, y al reprocesar una orden se compara el ID guardado contra el que devuelve BIMS: si divergen, se emite un error a log y a Sentry.

**Tech Stack:** Python 3.7 / Django / `unittest.mock` / `sentry_sdk`

**Spec:** `docs/superpowers/specs/2026-08-07-deteccion-venta-duplicada-design.md`

## Global Constraints

- **Producción corre Python 3.7.17.** Prohibido: `match/case`, uniones `X | Y` en anotaciones, walrus `:=`, f-strings con `=`, genéricos `list[]`/`dict[]`. Usar `typing.Optional`, `typing.List`.
- **Type hints obligatorios** en funciones nuevas (regla del `CLAUDE.md`).
- **Formato Black.** Correr `black .` antes de cada commit si está disponible.
- **Nunca HTTP real en tests.** Mockear con `unittest.mock`.
- **Comando de tests:** `.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings`
- **Baseline actual:** 59 tests en verde. Ninguna tarea puede bajar ese número.
- `bims.py` instancia `BimsApi()` al importarse; en tests se construye con
  `with patch.object(BimsApi, "login", return_value="fake_sid"): self.api = BimsApi()`.

---

## File Structure

| Archivo | Responsabilidad | Acción |
|---|---|---|
| `core/bims.py` | Detectar la respuesta de venta reutilizada y propagarla en el retorno de `create_sale` | Modificar |
| `core/models.py` | Guardar el `bims_sale_id` por orden | Modificar |
| `core/migrations/0006_failedorder_bims_sale_id.py` | Migración aditiva del campo | Crear (generada) |
| `core/services.py` | Comparar el ID previo contra el nuevo y alertar | Modificar |
| `core/tests.py` | Tests de las tres unidades | Modificar |
| `bims-api-reference.md` | Documentar el quirk de deduplicación | Modificar |
| `CLAUDE.md` | Nota sobre la dependencia y su vigilancia | Modificar |

---

### Task 1: Detección de venta reutilizada en `create_sale`

**Files:**
- Modify: `core/bims.py` (helper nuevo a nivel de módulo, junto a `_mask_login_body` en la línea ~57; y `create_sale` en las líneas 453-485)
- Test: `core/tests.py`

**Interfaces:**
- Consumes: nada de tareas anteriores.
- Produces:
  - `_es_venta_reutilizada(message: Optional[str]) -> bool`
  - `BimsApi.create_sale(...)` pasa de devolver `(sale_id, error_msg)` a devolver **`(sale_id, error_msg, reutilizada)`** — tupla de 3. La Task 3 depende de esta firma.

- [ ] **Step 1: Escribir el test que falla**

Agregar al final de `core/tests.py`:

```python
class CreateSaleDeduplicacionTest(TestCase):
    """BIMS deduplica por _id: un POST repetido devuelve la venta existente."""

    def setUp(self):
        with patch.object(BimsApi, "login", return_value="fake_sid"):
            self.api = BimsApi()

    def _respuesta(self, message):
        return {
            "code": "200",
            "status": "ok",
            "message": message,
            "data": {"Sale": {"id": "27442", "_id": "179134"}},
        }

    def _crear(self, respuesta):
        with patch.object(self.api, "_retry_request", return_value=respuesta):
            return self.api.create_sale(
                contact_id=15209,
                sale_products=[],
                posale_id=6,
                sales_payment_methods=[],
                contact_emails=None,
                order=179134,
            )

    def test_mensaje_de_venta_confirmada_marca_reutilizada(self):
        sale_id, error, reutilizada = self._crear(
            self._respuesta(
                "La venta no ha sido editada porque ya se encuentra confirmada en el servidor."
            )
        )
        self.assertEqual(sale_id, "27442")
        self.assertIsNone(error)
        self.assertTrue(reutilizada)

    def test_creacion_normal_no_marca_reutilizada(self):
        sale_id, error, reutilizada = self._crear(self._respuesta(None))
        self.assertEqual(sale_id, "27442")
        self.assertIsNone(error)
        self.assertFalse(reutilizada)

    def test_mensaje_desconocido_degrada_a_false(self):
        """Si BIMS cambia la redacción no rompemos: degradamos a reutilizada=False."""
        sale_id, error, reutilizada = self._crear(
            self._respuesta("Operación procesada correctamente.")
        )
        self.assertEqual(sale_id, "27442")
        self.assertFalse(reutilizada)

    def test_sin_sale_id_devuelve_error_y_reutilizada_false(self):
        """Falso positivo conocido: HTTP 200 sin data.Sale.id no es una venta."""
        respuesta = {"code": "200", "status": "ok", "message": "Sin stock", "data": {}}
        sale_id, error, reutilizada = self._crear(respuesta)
        self.assertIsNone(sale_id)
        self.assertEqual(error, "Sin stock")
        self.assertFalse(reutilizada)
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `.venv/bin/python manage.py test core.tests.CreateSaleDeduplicacionTest --settings=muci-integrador.test_settings -v 2`

Expected: FAIL con `ValueError: not enough values to unpack (expected 3, got 2)`.

- [ ] **Step 3: Agregar el helper de detección en `core/bims.py`**

Insertar a nivel de módulo, después de `_mask_login_body` (línea ~57) y antes de `class BimsError`:

```python
# Marca observada en la respuesta de BIMS cuando un POST /sales/ reenvía un _id que
# ya tiene venta: no crea una segunda, devuelve la existente. Comportamiento NO
# documentado en la API. Si el mensaje cambia de redacción, esto degrada a False y
# el flujo sigue igual — la alerta real vive en services.py y no depende del texto.
_MARCA_VENTA_REUTILIZADA = "ya se encuentra confirmada"


def _es_venta_reutilizada(message: Optional[str]) -> bool:
    """True si BIMS devolvió una venta preexistente en lugar de crear una nueva."""
    if not isinstance(message, str):
        return False
    return _MARCA_VENTA_REUTILIZADA in message.lower()
```

Verificar que `Optional` ya esté importado en `core/bims.py` (lo está: se usa en `_alternate_base_url`).

- [ ] **Step 4: Modificar el retorno de `create_sale`**

En `core/bims.py`, reemplazar el bloque final de `create_sale` (desde `data = response_data.get("data")` hasta el `return None, error_msg`) por:

```python
        data = response_data.get("data")
        if isinstance(data, dict) and data.get("Sale") and data["Sale"].get("id"):
            sale_id = data["Sale"]["id"]
            reutilizada = _es_venta_reutilizada(response_data.get("message"))
            if reutilizada:
                bims_logger.info(
                    f"BIMS devolvió la venta existente {sale_id} para el pedido "
                    f"{order} (deduplicación por _id, no se creó una nueva)."
                )
            return sale_id, None, reutilizada

        error_msg = (
            response_data.get("message")
            or response_data.get("error")
            or "BIMS devolvió HTTP 200 pero no generó la venta (ID vacío)."
        )
        return None, error_msg, False
```

- [ ] **Step 5: Correr los tests y verificar que pasan**

Run: `.venv/bin/python manage.py test core.tests.CreateSaleDeduplicacionTest --settings=muci-integrador.test_settings -v 2`

Expected: PASS, 4 tests.

Nota: la suite completa va a fallar en este punto porque `core/services.py` todavía desempaqueta 2 valores. Se arregla en la Task 3. Si preferís mantener la suite verde en cada commit, hacé las Tasks 1 y 3 en un solo commit.

- [ ] **Step 6: Commit**

```bash
git add core/bims.py core/tests.py
git commit -m "feat(bims): create_sale informa si BIMS reutilizo una venta existente"
```

---

### Task 2: Persistir el `bims_sale_id` en `FailedOrder`

**Files:**
- Modify: `core/models.py` (clase `FailedOrder`, línea 19)
- Create: `core/migrations/0006_failedorder_bims_sale_id.py` (generada por Django)
- Test: `core/tests.py`

**Interfaces:**
- Consumes: nada.
- Produces: `FailedOrder.bims_sale_id` — `CharField(max_length=32, blank=True, null=True)`. La Task 3 lo lee y lo escribe.

- [ ] **Step 1: Escribir el test que falla**

Agregar al final de `core/tests.py`:

```python
class FailedOrderBimsSaleIdTest(TestCase):
    """El ID de la venta en BIMS se persiste por orden para detectar duplicados."""

    def test_guarda_y_recupera_el_bims_sale_id(self):
        from core.models import FailedOrder

        FailedOrder.objects.create(
            order_id=179134, status=FailedOrder.COMPLETED, bims_sale_id="27442"
        )
        registro = FailedOrder.objects.get(order_id=179134)
        self.assertEqual(registro.bims_sale_id, "27442")

    def test_es_opcional_para_ordenes_existentes(self):
        from core.models import FailedOrder

        FailedOrder.objects.create(order_id=179135, status=FailedOrder.FAILED)
        registro = FailedOrder.objects.get(order_id=179135)
        self.assertIsNone(registro.bims_sale_id)
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `.venv/bin/python manage.py test core.tests.FailedOrderBimsSaleIdTest --settings=muci-integrador.test_settings -v 2`

Expected: FAIL con `TypeError: FailedOrder() got an unexpected keyword argument 'bims_sale_id'`.

- [ ] **Step 3: Agregar el campo al modelo**

En `core/models.py`, dentro de `class FailedOrder`, después del campo `message`:

```python
    bims_sale_id = models.CharField(
        verbose_name="ID de venta en BIMS",
        max_length=32,
        blank=True,
        null=True,
        help_text="ID de la venta creada en BIMS. Permite detectar facturas duplicadas.",
    )
```

BIMS devuelve el ID como string (`"27442"`), por eso `CharField` y no `IntegerField`.

- [ ] **Step 4: Generar la migración**

Run:
```bash
.venv/bin/python manage.py makemigrations core --settings=muci-integrador.test_settings
```

Expected: crea `core/migrations/0006_failedorder_bims_sale_id.py` con un solo `AddField`.

Verificar que no arrastre otros cambios:
```bash
.venv/bin/python manage.py makemigrations --check --dry-run --settings=muci-integrador.test_settings
```
Expected: `No changes detected`.

- [ ] **Step 5: Correr los tests y verificar que pasan**

Run: `.venv/bin/python manage.py test core.tests.FailedOrderBimsSaleIdTest --settings=muci-integrador.test_settings -v 2`

Expected: PASS, 2 tests.

- [ ] **Step 6: Commit**

```bash
git add core/models.py core/migrations/0006_failedorder_bims_sale_id.py core/tests.py
git commit -m "feat(models): campo bims_sale_id en FailedOrder para detectar duplicados"
```

---

### Task 3: Comparación y alerta en `services.py`

**Files:**
- Modify: `core/services.py` (helper nuevo antes de `process_order`, línea ~417; llamada a `create_sale` en línea 492; bloque de completado en línea 524)
- Test: `core/tests.py`

**Interfaces:**
- Consumes: `create_sale(...) -> (sale_id, error_msg, reutilizada)` de la Task 1; `FailedOrder.bims_sale_id` de la Task 2.
- Produces: `_verificar_venta_duplicada(order_id: int, sale_id_previo: Optional[str], sale_id_nuevo: str) -> bool`

- [ ] **Step 1: Escribir el test que falla**

Agregar al final de `core/tests.py`. Además, sumar `_verificar_venta_duplicada` a la lista de imports desde `core.services` que ya existe en la cabecera del archivo (dentro del bloque `with patch("requests.post")`):

```python
class VerificarVentaDuplicadaTest(TestCase):
    """Alerta cuando un mismo pedido termina con dos ventas distintas en BIMS."""

    def test_sin_venta_previa_no_alerta(self):
        with patch("core.services.sentry_sdk.capture_message") as mock_sentry:
            resultado = _verificar_venta_duplicada(179134, None, "27442")
        self.assertFalse(resultado)
        mock_sentry.assert_not_called()

    def test_mismo_id_no_alerta(self):
        """Caso normal de deduplicación: BIMS devolvió la misma venta."""
        with patch("core.services.sentry_sdk.capture_message") as mock_sentry:
            resultado = _verificar_venta_duplicada(179134, "27442", "27442")
        self.assertFalse(resultado)
        mock_sentry.assert_not_called()

    def test_id_divergente_alerta_a_sentry(self):
        """Si BIMS dejó de deduplicar, el pedido tiene dos facturas: alarma."""
        with patch("core.services.sentry_sdk.capture_message") as mock_sentry:
            resultado = _verificar_venta_duplicada(179134, "27442", "99999")
        self.assertTrue(resultado)
        self.assertEqual(mock_sentry.call_count, 1)
        mensaje = mock_sentry.call_args[0][0]
        self.assertIn("179134", mensaje)
        self.assertIn("27442", mensaje)
        self.assertIn("99999", mensaje)
        self.assertEqual(mock_sentry.call_args[1]["level"], "error")

    def test_previo_vacio_no_alerta(self):
        """Una cadena vacía es 'sin registro previo', no una divergencia."""
        with patch("core.services.sentry_sdk.capture_message") as mock_sentry:
            resultado = _verificar_venta_duplicada(179134, "", "27442")
        self.assertFalse(resultado)
        mock_sentry.assert_not_called()
```

- [ ] **Step 2: Correr el test y verificar que falla**

Run: `.venv/bin/python manage.py test core.tests.VerificarVentaDuplicadaTest --settings=muci-integrador.test_settings -v 2`

Expected: FAIL con `ImportError: cannot import name '_verificar_venta_duplicada'`.

- [ ] **Step 3: Implementar el helper**

En `core/services.py`, insertar justo antes de `def process_order(order_id: int) -> dict:` (línea ~417):

```python
def _verificar_venta_duplicada(
    order_id: int, sale_id_previo: Optional[str], sale_id_nuevo: str
) -> bool:
    """
    Compara la venta que BIMS acaba de devolver contra la ya registrada para este
    pedido.

    BIMS deduplica por el campo `_id` (el order_id de WooCommerce): un POST repetido
    devuelve la venta existente en vez de crear otra. Ese comportamiento no está
    documentado, así que lo vigilamos: si para un mismo pedido aparece un sale_id
    distinto al que ya teníamos, se generaron dos facturas.

    Devuelve True si detectó la divergencia. No bloquea ni revierte nada: anular una
    factura ya emitida es una decisión contable.
    """
    if not sale_id_previo or sale_id_previo == sale_id_nuevo:
        return False

    alerta = (
        f"POSIBLE FACTURA DUPLICADA en la orden {order_id}: ya estaba registrada la "
        f"venta {sale_id_previo} en BIMS y ahora se recibió {sale_id_nuevo}. "
        f"BIMS pudo haber dejado de deduplicar por _id."
    )
    logger.error(alerta)
    sentry_sdk.capture_message(alerta, level="error")
    return True
```

`Optional` ya está importado en `core/services.py` (línea 4).

- [ ] **Step 4: Correr el test y verificar que pasa**

Run: `.venv/bin/python manage.py test core.tests.VerificarVentaDuplicadaTest --settings=muci-integrador.test_settings -v 2`

Expected: PASS, 4 tests.

- [ ] **Step 5: Cablear el helper dentro de `process_order`**

Tres ediciones en `core/services.py`:

**(a)** Justo antes del `try:` que llama a `create_sale` (línea ~491), leer el ID previo:

```python
    sale_id_previo = (
        FailedOrder.objects.filter(order_id=order_id)
        .values_list("bims_sale_id", flat=True)
        .first()
    )
```

**(b)** Desempaquetar tres valores en la llamada (línea 492):

```python
        sale_id, bims_error, reutilizada = bims.create_sale(
```

**(c)** Después del bloque `if not sale_id:` y antes de `status_message = "Procesado con éxito."` (línea ~515), comparar; y agregar el campo en el `update_or_create` de completado (línea ~524):

```python
    sale_id_nuevo = str(sale_id)
    _verificar_venta_duplicada(order_id, sale_id_previo, sale_id_nuevo)
    if reutilizada:
        logger.info(
            f"Order {order_id}: BIMS devolvió la venta ya existente {sale_id_nuevo} "
            f"(deduplicación por _id)."
        )
```

Y en el `update_or_create` final, agregar la clave al dict `defaults`:

```python
    FailedOrder.objects.update_or_create(
        order_id=order_id,
        defaults={
            "status": FailedOrder.COMPLETED,
            "message": status_message,
            "bims_sale_id": sale_id_nuevo,
        },
    )
```

- [ ] **Step 6: Correr la suite completa**

Run: `.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings`

Expected: OK, 69 tests (59 previos + 4 de Task 1 + 2 de Task 2 + 4 de Task 3).

- [ ] **Step 7: Verificar compatibilidad con Python 3.7**

Run: `/usr/bin/python3.6 -S -m compileall -q core/`

Expected: exit 0, sin errores de sintaxis.

- [ ] **Step 8: Commit**

```bash
git add core/services.py core/tests.py
git commit -m "feat(services): alerta ante sale_id divergente para un mismo pedido"
```

---

### Task 4: Documentación

**Files:**
- Modify: `bims-api-reference.md` (sección "Quirks y comportamientos no obvios", después de "Falso positivo en create_sale", línea ~84)
- Modify: `CLAUDE.md` (descripción de `core/bims.py` en la sección de Arquitectura)

**Interfaces:**
- Consumes: el comportamiento implementado en las Tasks 1-3.
- Produces: nada de código.

Nota: `bims-api-reference.md` está sin trackear hoy. Este commit lo incorpora al repo. **Antes de agregarlo, confirmar que no contiene credenciales** (`grep -iE 'password|token|sid=' bims-api-reference.md`) — el repo es público.

- [ ] **Step 1: Documentar el quirk en `bims-api-reference.md`**

Agregar a la sección de quirks:

```markdown
### Deduplicación por `_id` en create_sale

Si se hace `POST /sales/` con un `_id` que ya tiene una venta asociada, BIMS **no crea
una segunda venta**: devuelve la existente con HTTP 200 y el mensaje

> `"La venta no ha sido editada porque ya se encuentra confirmada en el servidor."`

El campo `data.Sale.id` trae la factura original. Esto vuelve idempotentes los
reintentos de `_retry_request` ante timeouts o 502.

**Evidencia** (histórico de logs a 2026-08-07): 334 respuestas de venta sobre 286
pedidos distintos — 48 fueron reenvíos del mismo `_id`, 27 devolvieron el mensaje de
venta ya confirmada, y **ningún pedido generó más de una factura**.

⚠️ Este comportamiento **no está documentado por BIMS**. El integrador depende de él,
así que lo vigila: `_verificar_venta_duplicada` en `core/services.py` compara el
`bims_sale_id` guardado contra el que devuelve BIMS y alerta a Sentry si divergen.
```

- [ ] **Step 2: Actualizar `CLAUDE.md`**

En la descripción de `core/bims.py`, agregar al final del párrafo:

```markdown
`create_sale` devuelve `(sale_id, error, reutilizada)`; `reutilizada=True` indica que
BIMS deduplicó por `_id` y devolvió una venta preexistente en vez de crear otra
(comportamiento no documentado de BIMS, ver `bims-api-reference.md`). `FailedOrder`
guarda el `bims_sale_id` y `services._verificar_venta_duplicada` alerta a Sentry si un
mismo pedido llega a tener dos ventas distintas.
```

- [ ] **Step 3: Verificar que la doc no expone credenciales**

Run: `grep -inE 'password|token|sid=[a-z0-9]|github_pat' bims-api-reference.md`

Expected: solo menciones genéricas tipo `sid=<session_id>`, ningún valor real. Si aparece un valor concreto, reemplazarlo por un placeholder antes de commitear.

- [ ] **Step 4: Correr la suite completa una última vez**

Run: `.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings`

Expected: OK, 69 tests.

- [ ] **Step 5: Commit**

```bash
git add bims-api-reference.md CLAUDE.md
git commit -m "docs: quirk de deduplicacion por _id en BIMS y su vigilancia"
```

---

## Verificación final

- [ ] `.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings` → OK, 69 tests
- [ ] `.venv/bin/python manage.py makemigrations --check --dry-run --settings=muci-integrador.test_settings` → `No changes detected`
- [ ] `/usr/bin/python3.6 -S -m compileall -q core/` → exit 0
- [ ] `.venv/bin/python manage.py check --settings=muci-integrador.test_settings` → 0 issues

## Notas de deploy

La migración `0006` es aditiva y nullable — no toca datos existentes. Antes de deployar,
verificar el estado en el servidor con:

```bash
python manage.py showmigrations core
```

Las órdenes ya procesadas quedan con `bims_sale_id = NULL`. La detección empieza a
funcionar recién en el segundo procesamiento de cada orden posterior al deploy, que es
exactamente el escenario de reintento que se quiere vigilar.
