# Detección de facturas duplicadas en BIMS

**Fecha:** 2026-08-07
**Rama:** `feature/deteccion-venta-duplicada`
**Estado:** Diseño aprobado

## Contexto

`create_sale` envía el POST a `/sales/` a través de `_retry_request`, que reintenta
hasta 5 veces sobre la URL primaria y hasta 5 más sobre `BIMS_FALLBACK_URL`. Ante un
502 o un timeout posterior a que BIMS ya persistió la venta, el reintento reenvía el
mismo POST. La preocupación era que eso generara una segunda factura para el mismo
pedido de WooCommerce.

### Hallazgo: BIMS ya deduplica por `_id`

El análisis del histórico completo de logs (8 archivos, ~14 MB) descarta ese riesgo:

```
respuestas de venta parseadas : 334
pedidos distintos (_id)       : 286
respuestas 'ya confirmada'    : 27
duplicados                    : 0
```

De los 334 POSTs, 48 fueron reenvíos del mismo pedido — el escenario exacto que se
temía. En 27 de esos casos BIMS respondió:

```json
{
  "code": "200",
  "status": "ok",
  "message": "La venta no ha sido editada porque ya se encuentra confirmada en el servidor.",
  "data": { "Sale": { "id": "27442", "_id": "179134", ... } }
}
```

Es decir: BIMS reconoce el `_id` (el `order_id` de WooCommerce), se niega a crear una
venta nueva y devuelve la factura original. **Ningún `_id` generó más de un `Sale.id`.**

El mecanismo está bien alimentado: `_id` recibe `order_id`, parámetro obligatorio de
`process_order(order_id: int)` que viene del webhook, y nunca va vacío. `create_sale`
lee `data.Sale.id`, que en el caso deduplicado trae la factura existente, y lo reporta
como éxito — comportamiento correcto.

### El riesgo que sí queda

Esta protección es **un comportamiento no documentado de BIMS**. No figura en
`bims-api-reference.md`; se dedujo de los logs. Si una actualización de BIMS lo
cambiara, el integrador empezaría a duplicar facturas **en silencio**: hoy la respuesta
deduplicada se procesa igual que una creación normal, así que nada lo delataría.

## Objetivo

Si BIMS deja de deduplicar por `_id`, enterarnos el mismo día — no en una conciliación
contable meses después.

Fuera de alcance: impedir la duplicación. La deduplicación de BIMS funciona y se
verificó sobre 286 pedidos. Esto es una red de detección, no un reemplazo.

## Diseño

### 1. Distinguir "venta creada" de "venta reutilizada" — `core/bims.py`

`create_sale` pasa de devolver `(sale_id, error)` a `(sale_id, error, reutilizada)`.

`reutilizada` es `True` cuando BIMS devolvió una venta preexistente en lugar de crear
una nueva. Se detecta por el `message` de la respuesta (la marca observada es
`"ya se encuentra confirmada"`). La detección debe ser tolerante: si el mensaje cambia
de redacción, `reutilizada` queda en `False` y el resto del flujo sigue funcionando —
degrada a la conducta actual, no rompe.

Hay un único llamador (`core/services.py`), así que el cambio de firma está contenido.

### 2. Persistir el `bims_sale_id` — `core/models.py` + migración `0006`

Campo nuevo en `FailedOrder`, que ya lleva un registro único por `order_id`:

```python
bims_sale_id = models.CharField(
    verbose_name="ID de venta en BIMS", max_length=32, blank=True, null=True
)
```

Aditivo y nullable: sin riesgo para las filas existentes. Se completa al marcar
`COMPLETED`.

Nota: BIMS devuelve el ID como string (`"27442"`), de ahí `CharField`.

### 3. La detección — `core/services.py`

Al procesar una orden que **ya tiene** `bims_sale_id` guardado, comparar contra lo que
devuelve BIMS:

| Situación | Acción |
|---|---|
| No había `bims_sale_id` previo | Guardar el recibido. Flujo normal. |
| Coincide con el guardado | `logger.info`. La deduplicación funcionó. |
| **Difiere del guardado** | `logger.error` + `sentry_sdk.capture_message(level="error")` |

El tercer caso es la alarma que hoy no existe: significa que un mismo pedido produjo
dos facturas distintas en BIMS. El mensaje debe incluir `order_id`, el ID previo y el
nuevo, para que la conciliación sea inmediata.

La detección no bloquea ni revierte nada — la segunda factura ya existe en BIMS y
anularla es una decisión contable, no automatizable desde acá. En los tres casos la
orden se marca `COMPLETED` igual que hoy, y `bims_sale_id` se actualiza al último ID
devuelto; lo único que cambia en el caso divergente es que además se emite la alerta.

### 4. Tests — `core/tests.py`

Con `unittest.mock`, sin HTTP real:

1. **Contrato de deduplicación**: respuesta con `message` de venta ya confirmada →
   `create_sale` devuelve el `sale_id` existente y `reutilizada=True`.
2. **Creación normal**: respuesta sin ese mensaje → `reutilizada=False`.
3. **Divergencia**: orden con `bims_sale_id` previo distinto al devuelto → se dispara
   la alerta a Sentry.
4. **Coincidencia**: mismo ID → no se dispara alerta.
5. **Persistencia**: al completar, `FailedOrder.bims_sale_id` queda guardado.

### 5. Documentación

- `bims-api-reference.md`: agregar el quirk a la sección de comportamientos no obvios,
  con el mensaje exacto y la evidencia (286 pedidos, 48 reenvíos, 0 duplicados).
- `CLAUDE.md`: nota en la sección de BIMS sobre la dependencia de este comportamiento
  y la alerta que lo vigila.

## Criterios de aceptación

- [ ] `create_sale` distingue venta creada de venta reutilizada.
- [ ] Un cambio en la redacción del mensaje de BIMS degrada a `reutilizada=False`,
      sin romper el procesamiento.
- [ ] `FailedOrder.bims_sale_id` se persiste al completar una orden.
- [ ] Un `sale_id` divergente para un mismo `order_id` genera log de error y evento
      en Sentry.
- [ ] La suite completa pasa (59 tests actuales + los nuevos).
- [ ] El código compila bajo Python 3.6 (cota inferior de producción, 3.7.17).

## Riesgos

| Riesgo | Mitigación |
|---|---|
| El mensaje de BIMS cambia de redacción | La detección degrada a `False`; el punto 3 sigue alertando por comparación de IDs, que no depende del mensaje |
| El deploy requiere `migrate` | Migración aditiva y nullable; verificar con `showmigrations core` antes |
| Falsos positivos si un `order_id` se reusa entre tiendas | Fuera de alcance: hoy hay una sola tienda WooCommerce |
