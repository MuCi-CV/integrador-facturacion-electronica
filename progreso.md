# Progreso — Sub-proyecto A: ingreso, cola y estado por rama

**Spec:** `docs/superpowers/specs/2026-08-31-hub-ingreso-cola-estado-design.md`
**Plan:** `docs/superpowers/plans/2026-08-31-hub-ingreso-cola-estado.md`
**Rama:** `feature/hub-ingreso-cola`
**Actualizado:** 2026-09-02 — ✅ **SUB-PROYECTO A CERRADO.** Las 9 tareas hechas, los dos
despliegues en producción y verificado con una venta real. `main` @ `8ad502f` sirviendo tráfico.

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
eran el conjunto que la Tarea 7 tenía que verificar contra WooCommerce. **Verificado el 2026-09-02,
y el resultado cambió el diseño de la Tarea 7:** ya eran 82 (crecen una por venta) y **las 82 tenían
la meta correcta en Woo — 0 realmente faltantes, 0 valores distintos**. Ver abajo.

**Rollback:** código a **`43fd813`** y `systemctl restart`. Las migraciones se pueden dejar
aplicadas — una columna nueva que el código viejo ignora es inofensiva.

⚠️ **Hueco de cobertura asumido a conciencia:** la suite corre sobre SQLite en memoria y los ensayos
con datos también fueron sobre SQLite, así que el `AlterUniqueTogether` se ejecutó por primera vez
contra MariaDB **en producción**. Se aceptó porque era un `ADD UNIQUE` estándar sobre 8702 filas con
0 duplicados medidos, el índice pesa 288 bytes (muy bajo el límite de InnoDB), la guarda aborta
antes de tocar el esquema, y había dump. Salió bien; el hueco queda anotado por si el próximo
cambio de esquema es más grande.

✅ **Confirmado el 2026-09-02 con 18 ventas reales**, no con una: las 18 del 01/09 quedaron con
`external_reference = order_id`, `origin='woo'`, `status=COMPLETED` y **los dos identificadores de
BIMS llenos**. Sobre las 8716 filas de hoy: 0 sin referencia, 0 discrepancias, 0 duplicados, 0
`order_id` nulos. El Despliegue 1 queda cerrado y validado.

---

## Medición sobre producción del 2026-09-02 — cierra el Despliegue 1 y corrige la Tarea 7

Cruce read-only de las dos bases (`muci-integrador` y `muci`) con el usuario MySQL
`anthropic_readonly`.

| chequeo | resultado |
|---|---|
| las 18 ventas del 01/09 con identidad completa | **18 de 18** |
| filas totales / sin referencia / discrepancias / duplicados | 8716 / **0** / **0** / **0** |
| por estado | 201 `FAILED`, 8515 `COMPLETED` |
| filas `woo_meta_ok=False` con `bims_sale_id` | 82 |
| de esas 82, **cuántas ya tenían la meta en Woo** | **82** (0 faltantes, 0 valores distintos) |

**Las dos consecuencias de diseño**, las dos aplicadas en la Tarea 7:

1. **Nadie ponía `woo_meta_ok=True` al anotar.** Ni el código desplegado, ni la rama, ni el plan: el
   único escritor era la migración de backfill. Cada venta nueva nacía con el flag en `False`, así
   que la cola de reparación **crecía una fila por venta para siempre** (68 el 01/09, 82 el 02/09).
   Sin arreglarlo, `_repair_woo_metas` no converge nunca.
2. **La reparación tiene que leer antes de escribir.** Con el PUT directo del plan, esas 82 filas
   habrían sido **82 escrituras inútiles**, y cada `PUT` dispara `order.updated`, que despierta al
   bot de WhatsApp por una orden vieja. El `GET` no tiene ese efecto.

### `bims_invoice_number` viene en dos series, y no es clave única global

| serie | punto de venta | `payment_method` | ventas desde el 28/08 |
|---|---|---|---|
| 12xxx | boletería física | `fooeventspos-*` | 47 |
| 14xxx | web | `pagopar_*` | 35 |

Hoy los rangos no chocan por dónde están parados, no por garantía. **Importa para el sub-proyecto
C**, que es el que le manda ese número al CRM.

### Hueco de facturación: 58 pedidos que nunca llegaron al integrador

`wc-completed`, monto > 0, sin fila, desde el 2025-10-13: **58 pedidos, Gs 7.268.027**. Se desglosa
en dos cosas distintas:

| | pedidos | monto | qué es |
|---|---|---|---|
| Cortesía | 20 | 2.878.027 | **no es una falla**: hasta el 27/08 el código las descartaba a propósito |
| Pago cobrado | **38** | **4.390.000** | **la pérdida silenciosa real** |

Los 58 están hoy en `wc-completed`, **0 cancelados y 0 con devolución**. El 09/07 se perdieron **13
de 17** pedidos con RUC/CI y **0 de 19** sin documento; el 10/07, ya normalizado, 0 de ambos grupos.
**La causa técnica no está determinada:** quedaron descartadas la migración faltante
(`core_ruccache` ya existía desde el 08/07 14:43:55) y `ruc.py` (sin cambios desde el 29/06, y ya
era fail-safe).

**Y hay algo que vale más que el monto:** el 192578 y el 192584 tuvieron **10 y 2 cambios de estado
por edición masiva** entre el 14 y el 16/07 — alguien toggleando el estado para forzar el reenvío.
El reenvío **sí llegó** las dos veces, resolvió el contacto del comprador (creó los contactos BIMS
17573 y 17575) y **se cortó sin una línea de error y sin dejar fila**, mientras órdenes del mismo
minuto completaban normal. Es el camino de `SalesView` que devuelve 503 y no persiste nada: **el
riesgo vivo de A, documentado con nombre y número de orden.**

Planilla entregada a Finanzas el 2026-09-02 (xlsx + csv, 16 columnas, tres hojas). La columna
"Facturado en BIMS" dice **"Pendiente de verificar"**: el chequeo automático contra BIMS quedó
inválido —el listado de la API devolvió **18 ventas de más de 31.000**— y poner "no facturado" ahí
sería pasarle a Finanzas una conclusión falsa. Queda pendiente rehacer ese chequeo con la forma
correcta del endpoint (`/api/sales/index.json`, que acepta `limit`, `offset` y `method=count`).

---

## Lo que sigue: sincronización de stock BIMS → WooCommerce

Sub-proyecto nuevo, con sus propios documentos. **Implementado y en `main` @ `8996a71`, SIN
desplegar y en modo seco.**

- **Spec:** `docs/superpowers/specs/2026-09-02-sincronizacion-stock-bims-design.md`
- **Plan:** `docs/superpowers/plans/2026-09-02-sincronizacion-stock-bims.md` — 10 tareas, 51 pasos, todos cerrados

**288 tests** en local, en el stack de rollback y en el stack real. **Sin migraciones.** Arranca con
`STOCK_SYNC_ENABLED=false`, así que desplegar el código **no cambia nada en la web**.

⚠️ **La secuencia de despliegue tiene un paso que no se saltea:**

1. Subir el código con el flag apagado y **sin** la línea de cron.
2. **Correr `manage.py sync_stock` a mano, en seco, y leer la lista completa.** Es la única
   oportunidad de confirmar que los depósitos `6,7` son los correctos antes de que lo vea un
   cliente, y de ver cuántas variaciones se descartan por SKU ambiguo (medido entre 16 y 32; el
   barrido lo va a contar de verdad).
3. Con esa lista aprobada, instalar el cron
   (`*/15 * * * * root /var/www/integrador/sync-stock.sh`) y **recién después**
   `STOCK_SYNC_ENABLED=true`.

Se espera que el primer barrido **encienda** decenas de productos, no que apague:
`JUGUETE CARTAS INFANTILES SC` dice 16 en la web y tiene **71** en BIMS.

---

## Dónde encaja esto

El objetivo de negocio es que **el CRM sepa si una donación se facturó de verdad**, porque
fundraising va a cargar donaciones desde Krayin y esas nunca pasan por WooCommerce. A es el cuello:
**C** (el CRM recibe el nº de factura) y **F** (la entrada de donaciones) dependen de él.

| | sub-proyecto | estado |
|---|---|---|
| A′ | Guardar el `sale_id` y la factura | ✅ desplegado 2026-08-28 |
| **A** | Ingreso + cola + estado por rama + 202 + alerta | ✅ **desplegado 2026-09-02** |
| B | Modelo interno de pedido + cliente de origen | sin empezar |
| C | Rama CRM: escribir el lead y devolver el nº de factura | depende de A |
| D | Reintentos con backoff por rama | absorbido en A |
| — | **Stock BIMS → Woo** (no estaba en la lista original) | implementado, sin desplegar |
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
| 7 | Reintentos por rama | ✅ **hecha** | `9e264b3` |
| 8 | Alerta a Slack + corrección del logging | ✅ **hecha** | `a756906` |
| 9 | 🚀 Despliegue 2 — el cambio de contrato | ✅ **desplegado 2026-09-02 18:40 UTC** | `8ad502f` |

**Tests:** 183 (base) → 186 (T1) → 193 (T2) → 201 (T4) → 209 (T5) → 217 (T6) → 228 (T7) → **241** (T8).

### ✅ Despliegue 2 — HECHO el 2026-09-02 18:40:31 UTC, `main` @ `8ad502f`

Fast-forward desde `7704044`. Migraciones **`0012`** (no-op, 0 filas) y **`0013`**
(`AlertThrottle`) aplicadas. Cron del worker instalado en `/etc/crontab`, corriendo cada minuto.
`POST /sales/` ya devuelve **202**.

⚠️ **El plan decía "sin `migrate`" y era FALSO.** Producción estaba en la `0011` y faltaban dos. Sin
`migrate` el worker revienta al primer aviso, porque `core_alertthrottle` no existe. Runbook
corregido en `docs/2026-09-02-despliegue-2-runbook.md`.

**Verificado antes:** 241 OK sobre el stack real (3.10.12 + Django 5.2.17, corrida de Carlos) y sobre
el de rollback. **Después:** las dos migraciones registradas, los 3 workers con el código nuevo
(mtime 18:40:30.49 anterior al arranque 18:40:31), y la cola en 0/0/0.

**✅ Verificado con venta real a las 19:33.** Orden **205290** (Gs 180.000, Pago QR): entró como
`PENDING` 19:33:07 y quedó `COMPLETED` 19:34:23 — **76 segundos**, que son el minuto del cron más
~16 s de trabajo. `bims_sale_id 31422`, factura **14610** (serie 14xxx = web, coincide con el patrón
medido a la mañana), **`woo_meta_ok=1`**, las dos metas en Woo y el `_krayin_lead_id` intacto.

**Las dos pruebas deliberadas, las dos como estaban previstas:**

1. **La reparación de metas drenó el backlog de 76 filas en cuatro pasadas** (`20, 20, 20, 16`) con
   **CERO escrituras a WooCommerce**: las metas ya estaban. Se predijo
   `0 anotada(s), 20 ya estaba(n)` antes de correrlo y salió textual. Con el PUT directo del plan
   habrían sido 76 escrituras inútiles y 76 `order.updated` despertando al bot.
2. **Alerta probada a propósito** con la orden inexistente `999999999` y 4 intentos gastados: quedó
   `FAILED` con 5 intentos y el mensaje llegó al canal. **La fila en `core_alertthrottle` es la
   prueba de que el POST salió**, porque la marca se escribe DESPUÉS de que Slack contesta.

**Rollback:** sacar el cron, `git reset --hard 7704044`, `systemctl restart`. Las migraciones se
quedan. ⚠️ Las filas en `PENDING`/`PROCESSING` quedarían **huérfanas** — ver el runbook.

**Dos cabos menores, abiertos:** `/var/log/process-queue.log` **no tiene logrotate** (crece ~80 KB
por día), y `settings.py:145-147` **duplica cada línea en stdout** (`handlers: [console, file]` con
`propagate: True`, y el root también tiene console). Lo segundo es preexistente.

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

### Desvíos del plan en la Tarea 7 — decididos el 2026-09-02

Los tres salen de la medición de arriba, no de una opinión:

1. **`process_order` marca `woo_meta_ok=True` al anotar.** Nadie lo hacía; sin esto la pasada de
   reparación no converge nunca.
2. **`_repair_woo_metas` lee antes de escribir.** Evita 82 `PUT` inútiles y sus 82 `order.updated`.
   De paso queda auto-corrector: sirve para cualquier desincronización del flag, no solo para las 82
   de hoy.
3. **`MAX_META_ATTEMPTS = 20` queda afuera.** El plan lo definía y **no lo usaba**, y no hay columna
   donde llevar la cuenta de esa rama: acotarlo costaría una migración para limitar algo que no
   duele.

**Un agujero propio, encontrado y cerrado con test:** `_schedule_retry` buscaba por
`external_reference`, así que una fila de la ventana del despliegue —referencia en NULL, tomada por
`order_id`— **no recibía su reintento** y quedaba `PROCESSING` hasta que pasara el reaper, sin contar
el intento, o sea sin llegar nunca a `FAILED`. Ahora usa **`buscar_fila`, nueva y pública en
`states.py`**, que hace el mismo rescate que `upsert_state`. Y `meta_confirmada` pasó a pública en
`woocommerce.py`: la regla de comparar como texto tiene que ser una sola para el `PUT` y para el
`GET`.

### Desvíos del plan en la Tarea 8 — decididos el 2026-09-02

1. ⚠️ **El throttle vive en la BASE (`AlertThrottle`, migración `0013`), no en
   `django.core.cache`.** **No hay `CACHES` configurado**, así que Django usa `LocMemCache`, que es
   **por proceso** — y el worker es un proceso nuevo cada minuto por cron. El `cache.add` del plan
   habría arrancado vacío en cada corrida: **60 mensajes por hora** durante una caída de BIMS, que
   es exactamente lo que el throttle viene a evitar. Hay test que lo fija limpiando el cache entre
   dos avisos. Es la misma razón por la que la cola vive en MariaDB.
2. **La marca de "ya avisé" se escribe DESPUÉS de que Slack contesta.** Si Slack está caído, el
   aviso no se da por hecho; el costo está acotado por la cadencia del cron.
3. **La medición de la cola va ANTES de tomar el lote.** Al final —como salía del plan— daba **0
   pendientes con 15 esperando**, porque las filas de la pasada ya están en `PROCESSING`: la cola se
   ve vacía justo cuando más cargada está. Lo encontró un test que falló.

⚠️ **Corrección al plan que no hay que perder: NINGÚN disparador detecta que el cron esté muerto.**
El plan se lo atribuía al tercero, pero es imposible desde adentro del cron — si el cron no corre,
este código no corre y no avisa nada. Hace falta un **latido externo** y quedó **fuera de alcance**.
Lo que el tercero sí detecta es que la cola no avanza **aunque el worker esté corriendo**.


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
- ✅ **La rama de Woo se repara sola desde la Tarea 7** (`9e264b3`), y la reparación **lee antes de
  escribir**. En producción todavía no: hasta el Despliegue 2, un fallo al anotar la meta solo deja
  un `WARNING`.
- ⚠️ **No hay `CACHES` configurado: `LocMemCache` es por proceso.** Cualquier cosa que se apoye en
  `django.core.cache` para coordinar entre corridas del cron **no funciona** — el worker es un
  proceso nuevo cada minuto. Por eso el throttle de alertas vive en la base.
- ⚠️ **Un test puede salir a la red y seguir verde.** La verificación en el stack de rollback delató
  que `WorkerDeColaTest` hacía un **GET HTTP real a WooCommerce**: la pasada de reparación de la
  Tarea 7 levantaba las filas `COMPLETED` que esas pruebas dejan de adorno, y **el fallo de DNS lo
  tragaba el `except` del lote**. Parcheado en `setUp` (`91f3dea`); de paso la suite bajó de 8,4 s a
  3,4 s. Al agregar una pasada nueva al worker, revisar qué filas de los tests viejos caen en su
  filtro.
- **`black` no está instalado y el código no está formateado con él.** Correrlo ahora enterraría
  cualquier diff bajo cientos de líneas de estilo. Va como commit aislado, después del Despliegue 2.

---

## Lo que falta decidir (es de negocio, no técnico)

1. **Qué campos exactos de la factura de BIMS le interesan al CRM.** Bloquea **C**, no A.
2. **Si las donaciones ya cargadas a mano se recuperan.** Bloquea el alcance de **F**.
3. **Si las 149 cortesías históricas con precio se facturan retroactivamente.** 20 de ellas están
   en el hueco de los 58 pedidos.
4. **Qué se hace con los 38 pedidos con pago cobrado (Gs 4.390.000) que nunca llegaron.** La
   planilla ya está en manos de Finanzas; falta verificar en BIMS cuáles se facturaron a mano.
5. **Si `AlertThrottle` se registra en el admin.** Sirve para ver por qué un aviso no salió y para
   silenciarlo a mano; hoy no está.
6. **Si se pone un latido externo** que detecte de verdad que el cron murió. La Tarea 8 no lo cubre
   y no puede cubrirlo.

## Deuda técnica anotada, no urgente

- **`bims.py` no tiene ningún método de lectura de ventas** — solo `create_sale`, `get_posales`,
  `get_contacts` y `list_contacts`. El sub-proyecto C va a necesitar un `get_sales()` para
  reconciliar, y hoy eso vive en un script del scratchpad que además paginaba mal.
- **El webhook `Refund order` sigue deshabilitado** (`failure_count 6`) y apunta a
  `muci-integrador.staging.girolabs.cloud/refunds/`, una URL de **staging**: nunca estuvo apuntando
  a producción. Mientras siga así, toda cancelación se resuelve a mano en los dos sistemas. BIMS
  tiene `POST /api/sales/cancel/{id}.json` para cuando se decida cerrarlo.

---

## Riesgo vivo que A viene a cerrar

`SalesView` devuelve **503 ante cualquier excepción** y Woo **deshabilita el webhook a las 5 fallas
seguidas**. Una caída de BIMS de cinco órdenes apaga `Venta Entrada` y **la facturación se corta en
silencio**. Ya le pasó a `Refund order`, que quedó con `failure_count 6`.

Mientras A no esté desplegado, ese riesgo sigue abierto.
