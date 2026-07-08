# Changelog — Integrador Facturación Electrónica
**Rama:** `feature/refactor-service-layer`
**Documentado:** 2026-05-13 15:45

---

## Resumen de cambios desde `main`

Los cambios en esta rama abarcan el período **junio 2025 – mayo 2026** e incluyen mejoras de robustez en la comunicación con BIMS, una refactorización mayor de la capa de servicio y correcciones de bugs críticos en producción.

---

## 2025-06-06 — Manejo de errores y SKU en Sales Integrator

**Commits:** `72e9177`, `65b602b`

- Implementa manejo explícito de errores al procesar órdenes de WooCommerce.
- Agrega validación de SKU en los ítems de venta antes de enviar a BIMS; ítems sin SKU o con SKU inválido son omitidos con log descriptivo.

---

## 2025-10-07 — Recuperación de contraseña (Forgot Password)

**Commits:** `ec9b6d8` → `e24218b` (5 iteraciones)

- Implementa el flujo de recuperación de contraseña para usuarios del integrador.

---

## 2026-03-13 — Campos vacíos, duplicidad extendida y logs

**Commits:** `94630ba`, `802a8d0`, `6ff85b8`, `1bbb403`, `0058902`

- Manejo de campos vacíos en billing/shipping de WooCommerce para evitar enviar datos inválidos a BIMS.
- Prevención de duplicidad extendida: no se crea un contacto si ya existe uno para ese documento.
- Logging ampliado en puntos críticos del flujo de sincronización.

---

## 2026-03-14 — ContactCache y reintentos de FailedOrder

**Commits:** `1d33cbe`, `6372c8d`, `9d07514`

- **`ContactCache`**: nuevo modelo que almacena en BD local el mapeo `email / document_id → bims_id`. Evita múltiples llamadas a la API de BIMS para contactos ya conocidos.
- Lógica de reintento asíncrono para órdenes fallidas (`FailedOrder`).
- Corrección de `AttributeError: NoneType has no attribute strip` al procesar campos nulos de contacto.

---

## 2026-03-17 — Marcado de fallos y auto-reintento de sincronización doble

**Commits:** `9e98142`, `14e7208`, `96e08b9`

- Agrega marcado explícito como fallido (`FailedOrder`) cuando una sincronización no puede completarse.
- Corrige bug en el reintento automático cuando BIMS tenía dos contactos vinculados a la misma orden.

---

## 2026-03-18 — Login en comunicación con BIMS y detección de falso positivo

**Commits:** `2560e3e`, `d1cf4d5`, `7c021f3`

- **`company_id`**: se agrega el campo `company_id: 1` al payload de contactos y ventas, requerido por BIMS.
- **Re-login automático** (`_request_with_relogin`): si una petición a BIMS falla por sesión expirada, el integrador hace login nuevamente y reintenta de forma transparente.
- Logger dedicado `bims_api` con enmascaramiento de credenciales (`sid`, `password`, `tenant`) en los logs.
- Funciones helper: `_get_caller_name`, `_safe_json`, `_mask_params`, `_mask_login_body`.
- **Detección de falso positivo en `create_sale`**: BIMS puede devolver HTTP 200 sin incluir el `id` de la venta generada. Ahora se verifica explícitamente que `data.Sale.id` exista antes de considerarlo éxito; de lo contrario se retorna el mensaje de error de BIMS.

---

## 2026-03-23 — Log del método de pago en ventas POS

**Commit:** `3466e99`

- Se registra en el log el método de pago detectado para ventas POS (FooEvents), facilitando el debugging de casos donde BIMS recibe el método incorrecto.

---

## 2026-03-26 — Refactor: Service Layer + fix bug de monto en pagos POS

**Commit:** `cde3184`

Este es el cambio más significativo de la rama.

### Archivos afectados
| Archivo | Cambio |
|---|---|
| `core/constants.py` | Nuevo. Centraliza mapeos de POS y métodos de pago FooEvents → BIMS. |
| `core/services.py` | Nuevo. Extrae toda la lógica de negocio de `views.py`. |
| `core/views.py` | Simplificado de ~538 líneas a 90. Solo parsea el request y delega. |
| `core/tests.py` | 16 tests unitarios para el service layer. |
| `muci-integrador/test_settings.py` | Settings mínimos para ejecutar tests sin `.env`. |

### Funciones en `core/services.py`
- `resolve_pos_and_payments(meta_data, total)` — determina si la orden es POS y construye los métodos de pago.
- `_parse_pos_payments(meta_data, total)` — parsea los pagos de FooEvents POS.
- `resolve_contact_id(order_id, meta_data, billing, shipping, is_pos)` — resuelve o crea el contacto en BIMS.
- `build_sale_products(order_id, line_items, fee_lines, discount)` — construye la lista de productos para BIMS.
- `process_order(order_id)` — orquesta el flujo completo de una orden.

### Bug corregido: monto $0 en pagos POS
FooEvents POS (actualización ~febrero 2026) empezó a enviar `"amount": 0` en pagos únicos en lugar de omitir el campo. El código anterior lo leía como monto real, haciendo que BIMS registrara la venta con efectivo por defecto. Ahora se detecta `amount == 0` en pago único y se usa el `total` de la orden.

---

## 2026-03-27 — Fix: type hints incompatibles con Python < 3.10

**Commit:** `c5a595c`

- Reemplaza `X | Y` (union type hint syntax de Python 3.10+) por `Optional[X]` / `Union[X, Y]` para mantener compatibilidad con Python 3.7 (versión en producción: CPython 3.7.17).

---

## 2026-03-31 — Fix: error 403 "No tienes acceso a este recurso" al editar contacto

**Commit:** `4fd1079`

### Contexto
En producción (Sentry issue `#7369835435`, orden `183527`), al procesar una orden cuyo contacto ya existía en BIMS, el integrador intentaba **crear** un contacto nuevo. BIMS respondía `403 "No tienes acceso a este recurso"`. El sistema reintentaba 5 veces antes de fallar, creando un `FailedOrder`.

### Cambios en `core/bims.py`
- **Fallo inmediato en 403**: `_retry_request` ahora detecta `code == "403"` en la respuesta y lanza excepción de inmediato, sin agotar los reintentos.
- **`find_contact(document_id, document_type)`**: nuevo método que busca un contacto existente y devuelve sus datos completos tal como están guardados en BIMS (id, name, document_id, document_type, emails).
- **`update_contact_email(contact_id, name, document_id, document_type, new_email)`**: nuevo método que actualiza el email de un contacto existente usando su `id` en el payload.

### Cambios en `core/services.py`
- `resolve_contact_id` ahora intenta **actualizar** el email del contacto si ya existe en BIMS, en lugar de crear uno nuevo.
- Si `create_contact` falla con 403, se hace fallback a `ContactCache` local por si otro request concurrente ya insertó el registro.

---

## 2026-05-13 11:51 — Fix: sesión expirada no detectada (401 en body JSON) + mejoras menores

**Commit:** `b3cf2aa`

### Contexto
BIMS devuelve **HTTP 200** con body `{'code': '401', 'status': 'error', 'message': 'Unauthorized'}` cuando la sesión expira, en lugar de HTTP 401. El mecanismo de re-login en `_request_with_relogin` solo chequeaba el status HTTP (`res.status_code == 401`), por lo que nunca se disparaba. El `sid` expirado quedaba activo en el singleton `bims = BimsApi()` hasta reiniciar el servidor, causando un cluster de órdenes fallidas.

### `core/bims.py` — Re-login al detectar 401 en body JSON
- Se reestructuró `_request_with_relogin`: el JSON del response ahora se parsea **antes** del check de sesión expirada.
- La condición de re-login pasó de solo `res.status_code == 401` a `res.status_code == 401 or response_body.get("code") == "401"`.
- Si el re-login tiene éxito, se reintenta la petición con el nuevo `sid` y se re-parsea el JSON de la segunda respuesta.
- Si el re-login también devuelve 401, la respuesta se retorna a `_retry_request` para su manejo normal.

### `core/services.py` — Robustez en escritura concurrente de `ContactCache`
- Las dos llamadas a `ContactCache.objects.update_or_create` en `resolve_contact_id` ahora están envueltas en `try/except IntegrityError`, evitando excepciones por condición de carrera cuando dos requests procesan al mismo contacto en paralelo.

### `core/models.py` + migración `0004`
- Se agrega `db_index=True` al campo `FailedOrder.status` para acelerar las consultas de reintentos que filtran por estado (`FAILED` / `COMPLETED`).
- Migración: `core/migrations/0004_add_index_failedorder_status.py`

### `core/woocommerce.py` + `muci-integrador/settings.py`
- `verify_ssl` en el cliente WooCommerce dejó de estar hardcodeado como `False`. Ahora se lee de la variable de entorno `WOOCOMMERCE_VERIFY_SSL` (default: `True`).
- Se agrega `RUC_URL` como variable de entorno opcional en `settings.py`, necesaria para la búsqueda de razón social por RUC.

---

## 2026-05-13 15:33 — Fix: type hint incompatible con Python < 3.10

**Commit:** `220bcd3`

- `core/services.py`: la función `_search_contact_in_bims` usaba `-> dict | None` (sintaxis de union types de Python 3.10+), lo que causaba `TypeError: unsupported operand type(s) for |` al importar el módulo en producción (Python 3.7.17).
- Corrección: se agrega `from typing import Optional` y se cambia la anotación a `-> Optional[dict]`.

---

## 2026-06-29 — Fix: fast-fail terminal en 403/401 de BIMS (Bug A)

**Commit:** `e7a9911`

### Contexto
`_retry_request` lanzaba `raise Exception` para `code == "403"` DENTRO del bloque `try`, donde era atrapado de inmediato por el `except Exception` del mismo método. El 403 (rechazo de negocio terminal) se reintentaba 5 veces en lugar de fallar al instante (~10 s perdidos en un webhook síncrono). Confirmado por el error de Sentry del 11-abr-2026 (orden `183527`).

### Cambios en `core/bims.py`
- Jerarquía de excepciones: `BimsError` → `BimsTransientError` (red/status no-ok → reintentable) y `BimsBusinessError` (403 / 401 de permisos → terminal).
- `_retry_request` reescrito: la inspección de `status`/`code` ocurre FUERA del `try`; el 403 lanza `BimsBusinessError` que el loop no atrapa. Solo se reintentan errores transitorios. No duerme tras el último intento.
- `_request_with_relogin`: re-lanza `BimsTransientError` en errores de red; si tras un relogin exitoso sigue viniendo `code 401`, lo trata como `BimsBusinessError` terminal (problema de permisos, no de sesión).

### Cambios en `core/services.py`
- `resolve_contact_id` detecta el rechazo por `isinstance(e, BimsBusinessError)`, manteniendo el match de string `"403"` como respaldo.

### Tests
- 6 tests nuevos (`RetryRequestTest`) en `core/tests.py`.

---

## 2026-06-29 — Feature: corrección automática de razón social por RUC

**Commits:** `851f396`, `07a1659`, `2e90bee`, `e61d14e`, `fa53340`

### Contexto
En el checkout de WooCommerce el cliente carga RUC y razón social a mano; es común que el RUC esté bien pero la razón social mal. Ahora el integrador la corrige consultando la API pública de RUC (turuc), del lado del backend.

### Cambios
- **`core/ruc.py`** (nuevo): `_fetch_from_api` (HTTP propio con `timeout`, fail-safe — nunca lanza) y `get_razon_social` (orquesta caché). Independiente de la maquinaria de BIMS.
- **`core/models.py`**: nuevo modelo `RucCache` (`ruc` con DV, `razon_social`, `checked_at`, `created_at`) + migración `0005_ruccache`. TTL de caché: 30 días.
- **`core/services.py`**: `resolve_contact_id` usa la razón social autoritativa cuando `document_type == "ruc"`; si la fuente falla o no hay match, mantiene el dato de WooCommerce. Nunca rompe el procesamiento de la orden.
- **`muci-integrador/settings.py`** + `test_settings.py`: nuevo setting `RUC_API_URL` (default `https://turuc.com.py`), separado del `RUC_URL` legado.

### Reglas de caché
- Fresco (<30 días) → valor cacheado, sin pegarle a la API.
- Vencido/ausente + API ok → valor nuevo, `checked_at = now()`.
- Vencido + API falla → valor viejo SIN renovar `checked_at` (reintenta en la próxima orden).
- Sin caché + API falla → datos de WooCommerce.
- Escritura de caché protegida contra `IntegrityError` concurrente (commit `fa53340`).

### Tests
- 12 tests nuevos (modelo, capa HTTP, lógica de caché, integración). Suite completa: 36/36 verde.

### Pendiente (no-código)
- `RUC_API_URL` en el `.env` del server es OPCIONAL (default ya apunta a turuc).
- Spec y plan: `docs/superpowers/specs/2026-06-29-razon-social-por-ruc-design.md` y `docs/superpowers/plans/2026-06-29-razon-social-por-ruc.md`.

---

## 2026-07-08 — Feature: URL secundaria de BIMS (`BIMS_FALLBACK_URL`) con conmutación automática

**Commit:** `4fb3524`

### Contexto
El 2026-07-08 `bims.app` quedó indisponible y BIMS habilitó `in.bims.app` como host alternativo. Además, `bims = BimsApi()` es un singleton a nivel de módulo que hace login al importar: con la URL primaria caída, Django directamente no arrancaba.

### Cambios en `core/bims.py`
- `_alternate_base_url()`: conmuta la instancia entre base primaria y secundaria (simétrico) y reescribe la URL. La conmutación es *sticky*: las siguientes llamadas de la instancia usan la base nueva, y al reiniciar el proceso se vuelve a la primaria.
- `_retry_request` ahora envuelve a `_retry_loop`: agota los `max_retries` (5) contra la base en uso y, solo si todos fallan por error transitorio, conmuta a la alternativa y reintenta ahí. Los rechazos terminales (403 / 401 de permisos) NO conmutan.
- `login()` conmuta a la alternativa si no logra conectar (evita que el boot del proceso falle con la primaria caída). Se agrega `timeout=30` al POST de login, que no tenía.

### Configuración
- `muci-integrador/settings.py` + `test_settings.py`: nuevo setting opcional `BIMS_FALLBACK_URL`. Vacía o ausente = comportamiento idéntico al anterior (solo los 5 reintentos contra `BIMS_URL`).
- `.env.example`: variable documentada.

### Tests
- 8 tests nuevos (`BimsFallbackUrlTest`) en `core/tests.py`. Suite completa: 44/44 verde.

### Deploy en producción (2026-07-08)
- Se activó con `BIMS_FALLBACK_URL=https://in.bims.app/api` en el `.env` del servidor + reinicio del servicio.
- Al deployar se detectó que el servidor tenía migraciones sin aplicar (`0004` y `0005`): la orden `192462` falló con error MySQL 1146 (`core_ruccache doesn't exist`). Se corrió `python manage.py migrate` y se recuperó con `runretryfaileds.sh`.

### Pendiente
- **Endurecer `get_razon_social` (`core/ruc.py`)**: un error de base de datos al leer/escribir `RucCache` (p. ej. tabla inexistente) hoy se propaga y marca la orden entera como fallida. Debería degradar a "sin razón social", igual que cuando falla la API de turuc.
- **Recordatorio operativo**: todo deploy debe incluir `python manage.py migrate` (el incidente 1146 salió de omitirlo).
