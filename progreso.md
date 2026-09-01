# Progreso — Sub-proyecto A: ingreso, cola y estado por rama

**Spec:** `docs/superpowers/specs/2026-08-31-hub-ingreso-cola-estado-design.md`
**Plan:** `docs/superpowers/plans/2026-08-31-hub-ingreso-cola-estado.md`
**Rama:** `feature/hub-ingreso-cola`
**Actualizado:** 2026-09-01 (cierre de sesión: Tareas 5 y 6 hechas, rama @ `55146bc` sin pushear)

## Medición sobre producción del 2026-09-01 — resuelve las dos incógnitas abiertas

| | resultado | consecuencia |
|---|---|---|
| `order_id` duplicados | **0** | el `unique_together` **es aplicable**: la `0011` entra en el Despliegue 1, ya no diferida |
| filas `"Pausada: Esperando"` | **0** | la `0012` es un **no-op confirmado**; el canal estaba muerto desde el 2026-03-17 |
| por estado | 201 `FAILED`, 8.501 `COMPLETED` | coincide con las 201 en `FAILED` del backlog viejo |
| total | **8.702** | la cifra de "8588" que repetían los docs estaba vieja; corregida en código y docs |

La guarda de la `0011` se queda igual: si entrara un duplicado entre la medición y la migración,
aborta limpio dejando la `0010` aplicada. Un chequeo de hace horas no es un invariante.

**Backup previo hecho:** `/root/bk/db-pre-expansion.sql.gz`, 226 MB, las 4 bases, dump verificado.
Es el primer dump que existe, y cierra el requisito que le faltaba a la Tarea 3-bis.

## Despliegue 1 — HECHO el 2026-09-01, `main` @ `7704044`

`0009`, `0010` y `0011` aplicadas. Verificación previa sobre el stack REAL (Python 3.10.12 +
Django 5.2.17): 193 OK. Verificación posterior sobre los datos:

| chequeo | resultado |
|---|---|
| total | 8702, idéntico al de antes |
| nulos en `external_reference` | **0** |
| discrepancias `str(order_id)` vs `external_reference` | **0** |
| `woo_meta_ok=True` | 8433 = exactamente las `COMPLETED` sin `bims_sale_id` |

**68 órdenes** (8501 − 8433) facturadas desde el 28/08 quedaron en `woo_meta_ok=False` a propósito:
son el conjunto que el reaper de la Tarea 7 va a verificar contra WooCommerce.

**Rollback:** código a **`43fd813`** y `systemctl restart`. Las migraciones se pueden dejar
aplicadas — una columna nueva que el código viejo ignora es inofensiva.

⚠️ **Hueco de cobertura asumido a conciencia:** la suite corre sobre SQLite en memoria y los ensayos
con datos también fueron sobre SQLite, así que el `AlterUniqueTogether` se ejecutó por primera vez
contra MariaDB **en producción**. Se aceptó porque era un `ADD UNIQUE` estándar sobre 8702 filas con
0 duplicados medidos, el índice pesa 288 bytes (muy bajo el límite de InnoDB), la guarda aborta
antes de tocar el esquema, y había dump. Salió bien; el hueco queda anotado por si el próximo
cambio de esquema es más grande.

**Falta:** confirmar con una venta real que `POST /sales/` sigue dando **200** con las dos columnas
de identidad llenas.

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
| 3 | 🚀 Despliegue 1 — solo esquema | ✅ **desplegado 2026-09-01** | `7704044` |
| 3-bis | Contraer: borrar `order_id` | 🔵 **diferida** a propósito | |
| 4 | `NOT_APPLICABLE` y `PAUSED` en uso | ✅ **hecha** | |
| 5 | Ingreso 202 + persistencia | ✅ **hecha** | `e2066a1` |
| 6 | Worker + reaper | ✅ **hecha** | `55146bc` |
| 7 | Reintentos por rama | ⬜ | |
| 8 | Alerta a Slack + corrección del logging | ⬜ | |
| 9 | 🚀 Despliegue 2 — el cambio de contrato | ⬜ | |

**Tests:** 183 (base) → 186 (T1) → 193 (T2) → 201 (T4) → 209 (T5) → **217** (T6).

⚠️ **Las Tareas 5 y 6 no se pueden desplegar por separado.** La 5 deja de facturar en línea y la 6
es lo único que vacía la cola: subir solo la 5 sería dejar de facturar del todo.

### Hallazgo de la Tarea 4: el canal `PAUSADA` ya estaba muerto

La spec §5 y el plan tratan `"Pausada: Esperando"` como un canal vivo. **No lo es:** el escritor de
ese mensaje se eliminó el **2026-03-17** en `96e08b9` (`core/views.py`), junto con el
`return Response({"status": "paused", ...})` que `sync_bims_contacts.py:83` todavía espera. Desde
marzo no se crean filas nuevas por ese camino. **Medido el 2026-09-01: quedan 0 filas**, así que la
`0012` es un no-op confirmado.

**Decidido por Carlos el 2026-09-01: se conserva y se convierte, no se borra.** La recomendación
había sido borrarlo (sin escritor desde marzo, 0 filas que atender), pero el bloque queda vivo con
el patrón nuevo: filtra `PAUSED` y encola. El costo es mantener un cuarto call site; el beneficio,
que el canal siga funcionando si `PAUSED` vuelve a tener escritor.

**Consecuencia en el código:** `PAUSED` tuvo que entrar en `REQUEUEABLE`, que el plan definía como
`(FAILED, NOT_APPLICABLE)`. Con esa lista el bloque no podía reencolar nada — el plan se contradecía
a sí mismo. `PAUSED` no es "ya se hizo" ni "ya está en la cola": es una orden trabada esperando un
contacto, que es justo lo que hay que reencolar.

### Desvíos del plan en la Tarea 2 — decididos el 2026-09-01

1. **El `unique_together` va en una migración aparte (`0011`), detrás de una guarda.** Hoy
   `order_id` **no tiene constraint único** y `update_or_create` es competible, así que puede haber
   `order_id` repetidos entre las 8702 filas. Tras el backfill esos duplicados rompen el constraint,
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

- ✅ **CUATRO consumidores se rompen con el 202 — CORREGIDO EN CÓDIGO** en `e2066a1` (Tarea 5). Los
  cuatro pasaron a escribir en la BD con `enqueue()` y los dos del admin ahora dicen "encolada(s)"
  en vez de "procesadas correctamente". **Ninguno de los cuatro tenía test**: la suite entera seguía
  verde con el ingreso ya convertido, que es exactamente la falla silenciosa que A viene a eliminar.
  Ahora los cubre `ReintentosEscribenEnLaColaTest`.
- ⚠️ **`runretryfaileds.sh` apunta a una ruta que NO existe** (`/var/www/integrador.muci.org/backend`;
  el checkout real es `/var/www/integrador`, verificado por SSH el 2026-09-01). Si esa es la ruta que
  usa el cron, el reintento de fallidas lleva tiempo sin correr. **No se pudo confirmar**: el crontab
  de root no es legible como `anthropic_readonly`. Lo tiene que mirar Carlos.
- ⚠️ **`SKIP LOCKED` no está cubierto por los tests.** Django lo ignora sin error en SQLite, así que
  la exclusión entre workers concurrentes **solo se ejerce en MariaDB**. Los tests cubren la lógica
  de selección y marcado, no la concurrencia.
- ⚠️ **Los scripts del cron necesitan `cd` al checkout.** `settings.py` carga la config con
  `dotenv_values(".env")`, que es ruta **relativa**: desde el home del cron no hay `.env` y settings
  revienta con `AttributeError: 'NoneType' object has no attribute 'lower'` antes de llegar a
  Django. `process-queue.sh` ya lo hace; comprobado en la sesión del 2026-09-01.
- ⚠️ **`makemigrations` va con `dev_settings`, no con `test_settings`.** `core/bims.py:723` tiene
  `bims = BimsApi()` a nivel de módulo y el `__init__` hace login: con `test_settings` el comando
  intenta conectarse a un host inventado y **crashea sin generar nada**.
- ⚠️ **`FAILED=1` y `COMPLETED=2` no se renumeran nunca.** Hay **8.702** filas en producción que
  dependen de esos valores (medido el 2026-09-01: 201 en `FAILED`, 8.501 en `COMPLETED`).
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
