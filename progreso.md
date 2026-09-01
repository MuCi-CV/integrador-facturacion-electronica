# Progreso — Sub-proyecto A: ingreso, cola y estado por rama

**Spec:** `docs/superpowers/specs/2026-08-31-hub-ingreso-cola-estado-design.md`
**Plan:** `docs/superpowers/plans/2026-08-31-hub-ingreso-cola-estado.md`
**Rama:** `feature/hub-ingreso-cola`
**Actualizado:** 2026-08-31

---

## Dónde encaja esto

El objetivo de negocio es que **el CRM sepa si una donación se facturó de verdad**, porque
fundraising va a cargar donaciones desde Krayin y esas nunca pasan por WooCommerce. A es el cuello:
**C** (el CRM recibe el nº de factura) y **F** (la entrada de donaciones) dependen de él.

| | sub-proyecto | estado |
|---|---|---|
| A′ | Guardar el `sale_id` y la factura | ✅ desplegado 2026-08-28 |
| **A** | **Ingreso + cola + estado por rama + 202 + alerta** | **en curso** |
| B | Modelo interno de pedido + cliente de origen | sin empezar |
| C | Rama CRM: escribir el lead y devolver el nº de factura | depende de A |
| D | Reintentos con backoff por rama | absorbido en A |
| E | Adaptador PrestaShop / Ticketera 2.0 | depende de B |
| F | Entrada de donaciones manuales desde Krayin | depende de A |

---

## Tareas

| # | tarea | estado | commit |
|---|---|---|---|
| 1 | Estados y campos de cola (aditivo) | ✅ **hecha** | `51f2798` |
| 2 | Identidad: expandir con `external_reference` | ✅ **hecha** | |
| 3 | 🚀 Despliegue 1 — solo esquema | ⬜ **siguiente** | |
| 3-bis | Contraer: borrar `order_id` | 🔵 **diferida** a propósito | |
| 4 | `NOT_APPLICABLE` y `PAUSED` en uso | ✅ **hecha** | |
| 5 | Ingreso 202 + persistencia | ⬜ | |
| 6 | Worker + reaper | ⬜ | |
| 7 | Reintentos por rama | ⬜ | |
| 8 | Alerta a Slack + corrección del logging | ⬜ | |
| 9 | 🚀 Despliegue 2 — el cambio de contrato | ⬜ | |

**Tests:** 183 (base) → 186 (T1) → 193 (T2) → **201** (T4) → ~215 esperados al terminar.

### Hallazgo de la Tarea 4: el canal `PAUSADA` ya estaba muerto

La spec §5 y el plan tratan `"Pausada: Esperando"` como un canal vivo. **No lo es:** el escritor de
ese mensaje se eliminó el **2026-03-17** en `96e08b9` (`core/views.py`), junto con el
`return Response({"status": "paused", ...})` que `sync_bims_contacts.py:83` todavía espera. Desde
marzo no se crean filas nuevas por ese camino, así que la migración `0012` es limpieza histórica y
puede ser un no-op según cuántas filas queden.

**Consecuencia a decidir en la Tarea 5:** con el filtro por estado y sin escritor, el bloque de
auto-reintento de pausadas de `sync_bims_contacts` queda comprobadamente muerto. O se le da un
escritor nuevo, o se borra — pero dejarlo como está es código que parece hacer algo y no hace nada.

### Desvíos del plan en la Tarea 2 — decididos el 2026-09-01

1. **El `unique_together` va en una migración aparte (`0011`), detrás de una guarda.** Hoy
   `order_id` **no tiene constraint único** y `update_or_create` es competible, así que puede haber
   `order_id` repetidos entre las 8588 filas. Tras el backfill esos duplicados rompen el constraint,
   y en MariaDB el DDL ya hizo commit → la `0010` quedaría a mitad de camino, que es justo lo que
   expandir/contraer venía a evitar. La guarda corre **antes de todo DDL** y aborta con los
   duplicados listados. **Ensayado sobre SQLite con datos:** aborta, deja el esquema consistente en
   `0010`, y aplica bien tras deduplicar.
2. **La migración de datos a `PAUSED` se difiere a la Tarea 4.** `sync_bims_contacts.py:63` filtra
   por `status=FAILED, message__startswith="Pausada: Esperando"`: flipear los estados sin cambiar
   ese filtro deja al comando **sin encontrar nada y sin avisar**. Viaja junto al cambio de código.
3. **`woo_meta_ok` se llena por dato, no por fecha.** El plan cortaba por
   `created_at < 2026-08-28`; ahora es `status=COMPLETED AND bims_sale_id IS NULL`, porque una fila
   sin `bims_sale_id` **no se puede anotar** — no tenemos el número que habría que escribirle. No
   depende de adivinar la hora del despliegue de A′ y el flag no afirma algo falso.
4. **`upsert_state` rescata la fila que dejó el código viejo.** En el despliegue, `migrate` corre
   con el código viejo todavía sirviendo: una venta en esa ventana deja `external_reference` en NULL
   y el código nuevo crearía una **segunda fila para la misma orden**. Se busca por referencia y, si
   no hay, por `order_id` con referencia nula.

---

## Decisiones que ya no se re-litigan

| decisión | cuándo | por qué |
|---|---|---|
| La cola vive en MariaDB, con cron y `flock` | 26/08 | Redis existe pero **no es nuestro**; MariaDB ya es el almacén propio y se ve desde el admin |
| Extender `FailedOrder`, no crear tabla nueva | 26/08 | dos tablas darían **dos fuentes de verdad sobre si una orden se facturó** |
| Alertas a Slack; Sentry para bugs de código | 26/08 | hoy cada `logger.error()` es un evento de Sentry y `bims.py` loguea uno por reintento |
| Generalizar la identidad ahora | 31/08 | la topología de **dos orígenes** ya está decidida y publicada: una fila sin `order_id` es certeza |
| **Expandir/contraer en vez de `RenameField`** | 31/08 | en MariaDB el DDL hace commit implícito → la atomicidad de Django **no cubre el esquema**; y no hay dump para ensayar |
| Backoff propio solo para la rama BIMS | 31/08 | la de Woo es barata e idempotente; una columna alcanza. Con backoff por rama serían 9 columnas al entrar el CRM |
| Identificadores en inglés, docs en español | 31/08 | pedido de Carlos |
| No paralelizar el worker todavía | 31/08 | no hay dato del pico; **la alerta de tamaño de cola es el instrumento que lo va a producir** |

---

## Trampas conocidas — leer antes de tocar

- ⚠️ **CUATRO consumidores se rompen con el 202** — ✅ **ya corregido en el plan (Tarea 5)**:
  `retryfaileds.py:28`, `sync_bims_contacts.py:78`, y en el admin `retry_failed_orders_button`
  (`:111`) y `retry_selected_orders` (`:152`). Los cuatro chequean `status_code == 200`, que con el
  202 **nunca vuelve a ser cierto**: no explotan, **dejan de hacer nada sin avisar**.
  Los dos del admin además le dicen al usuario "procesadas correctamente", así que la pantalla
  mentiría. El plan ahora corrige también ese mensaje, no solo el código.
- ⚠️ **`makemigrations` va con `dev_settings`, no con `test_settings`.** `core/bims.py:723` tiene
  `bims = BimsApi()` a nivel de módulo y el `__init__` hace login: con `test_settings` el comando
  intenta conectarse a un host inventado y **crashea sin generar nada**.
- ⚠️ **`FAILED=1` y `COMPLETED=2` no se renumeran nunca.** Hay 8.588 filas en producción que
  dependen de esos valores.
- ⚠️ **La rama de Woo se repara sola a partir de la Tarea 7.** Hasta entonces, un fallo al anotar la
  meta solo deja un `WARNING`.
- **`black` no está instalado y el código no está formateado con él.** Correrlo ahora enterraría
  cualquier diff bajo cientos de líneas de estilo. Va como commit aislado, después del Despliegue 2.

---

## Lo que falta decidir (es de negocio, no técnico)

1. **Qué campos exactos de la factura de BIMS le interesan al CRM.** Bloquea **C**, no A.
2. **Si las donaciones ya cargadas a mano se recuperan.** Bloquea el alcance de **F**.
3. **Si las 149 cortesías históricas con precio se facturan retroactivamente.**

---

## Riesgo vivo que A viene a cerrar

`SalesView` devuelve **503 ante cualquier excepción** y Woo **deshabilita el webhook a las 5 fallas
seguidas**. Una caída de BIMS de cinco órdenes apaga `Venta Entrada` y **la facturación se corta en
silencio**. Ya le pasó a `Refund order`, que quedó con `failure_count 6`.

Mientras A no esté desplegado, ese riesgo sigue abierto.
