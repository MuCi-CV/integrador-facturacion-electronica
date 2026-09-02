# Handoff — sesión del 2026-09-02

**Rama:** `feature/hub-ingreso-cola` @ `a756906` — **6 commits sin pushear, nada desplegado.**
Tres son de hoy (`9e264b3`, `91f3dea`, `a756906`). El upstream de la rama es
`github/feature/hub-ingreso-cola`; hay un segundo remoto `origin` al **mismo repo** cuya ref local
está más atrasada, así que un `git log origin/…` da una cuenta distinta y engañosa.
**Tests:** 241 OK en local (3.12 + Django 5.2.17) y **VERDE también sobre el stack de rollback**
(3.7 + Django 3.2), verificado en cada commit con `./verificar-en-stack-produccion.sh`.

**Lo siguiente es el Despliegue 2 (Tarea 9)**, que es el que cambia el contrato del ingreso. Las
Tareas 4 a 8 están listas en rama. Seguimiento vivo en `progreso.md`.

---

## 1. El Despliegue 1 quedó validado, con n=18 en vez de n=1

Lo que faltaba era "confirmar con una venta real". Se confirmó con **18**: las 18 ventas del 01/09
quedaron con `external_reference = order_id`, `origin='woo'`, `status=COMPLETED` y los dos
identificadores de BIMS llenos. Sobre las 8716 filas: **0 sin referencia, 0 discrepancias contra
`order_id`, 0 duplicados, 0 `order_id` nulos**.

El cruce contra WooCommerce cerró también la otra mitad: las **82** filas con `woo_meta_ok=False`
**ya tenían `_bims_sale_id` y `_bims_invoice_number` correctos en Woo** — 0 faltantes, 0 valores
distintos. Y la 204000, la que había perdido la meta el 31/08, ya la tiene.

**Acceso nuevo:** hay un usuario MySQL `anthropic_readonly` que da lectura sobre `muci-integrador`,
`muci`, `krayin` y `moodle`. La clave la tiene Carlos. Dos trampas al usarlo: las dos bases tienen
**collations distintas** (comparar columnas entre ellas tira `ERROR 1267`, hay que forzar
`COLLATE utf8mb4_general_ci` en las dos puntas) y el nombre de la base lleva guion, así que va entre
backticks. **No se lee el `.env` ni `wp-config.php`** — con este usuario no hace falta.

---

## 2. Tarea 7 — backoff para BIMS y auto-reparación de la meta (`9e264b3`, `91f3dea`)

La rama de BIMS espera `1, 5, 15, 60` minutos y a los 5 intentos deja de insistir con la fila en
`FAILED`. La de Woo se reintenta en cada pasada, sin contador.

**Los tres desvíos del plan salieron de la medición, no de una opinión** (detalle en `progreso.md`):

1. `process_order` ahora **marca `woo_meta_ok=True` al anotar**. Nadie lo hacía, así que la cola de
   reparación crecía una fila por venta para siempre y nunca convergía.
2. `_repair_woo_metas` **lee antes de escribir**. El PUT directo del plan habrían sido 82 escrituras
   inútiles, y cada una dispara `order.updated`, que despierta al bot de WhatsApp por una orden
   vieja.
3. `MAX_META_ATTEMPTS` **queda afuera**: el plan lo definía sin usarlo y no hay columna donde contar.

**Un agujero propio, cerrado con test:** `_schedule_retry` buscaba por `external_reference`, así que
una fila de la ventana del despliegue —referencia en NULL— no recibía reintento y quedaba
`PROCESSING` hasta el reaper, sin contar el intento. Ahora usa `buscar_fila`, nueva y pública en
`states.py`.

---

## 3. Tarea 8 — alertas a Slack y los reintentos fuera de Sentry (`a756906`)

`core/alerts.py` con `notify(clave, texto)`, fail-safe entero. Tres disparadores al final de cada
pasada: cola sobre el umbral, reintentos agotados (con los números de orden) y filas vencidas que no
avanzan. Migración `0013`: un `CREATE TABLE` limpio.

**El desvío que importa: el throttle vive en la base, no en `django.core.cache`.** No hay `CACHES`
configurado, así que Django usa `LocMemCache`, que es **por proceso** — y el worker es un proceso
nuevo cada minuto. El `cache.add` del plan habría arrancado vacío en cada corrida: **60 mensajes por
hora** durante una caída de BIMS.

⚠️ **Y una corrección al plan que conviene no perder: ningún disparador detecta que el cron esté
muerto.** El plan se lo atribuía al tercero, pero es imposible desde adentro del cron — si el cron
no corre, el aviso tampoco. Necesita un latido externo y quedó fuera de alcance. El tercero detecta
que la cola no avanza **aunque el worker corra**, que es otra cosa.

`core/bims.py:446` pasó de `logging.error` a `logging.warning`: con
`LoggingIntegration(event_level=ERROR)`, cada reintento transitorio era un evento de Sentry.

---

## 4. Hallazgo de negocio: 58 pedidos nunca llegaron al integrador

`wc-completed`, monto > 0, sin fila, desde el 2025-10-13: **58 pedidos, Gs 7.268.027**. Son dos
cosas distintas y conviene no mezclarlas:

| | pedidos | monto | qué es |
|---|---|---|---|
| Cortesía | 20 | 2.878.027 | **no es falla**: hasta el 27/08 el código las descartaba a propósito |
| Pago cobrado | **38** | **4.390.000** | la pérdida silenciosa real |

Los 58 están en `wc-completed`, **0 cancelados, 0 con devolución**. El más reciente es del 26/08:
desde el 27/08 no se perdió ninguno.

**El dato que más pesa:** el 192578 y el 192584 tuvieron 10 y 2 cambios de estado por edición masiva
entre el 14 y el 16/07 — alguien forzando el reenvío a mano. El reenvío **llegó** las dos veces,
resolvió el contacto y **se cortó sin error y sin dejar fila**, mientras órdenes del mismo minuto
completaban normal. Es el 503-sin-persistir de `SalesView`, con nombre y número de orden.

**Planilla entregada a Finanzas** (xlsx + csv, ya enviada). La columna "Facturado en BIMS" dice
**"Pendiente de verificar"**, y eso es a propósito: el chequeo automático contra BIMS **quedó
inválido** porque el listado devolvió **18 ventas de más de 31.000**. No tomar esa corrida como
evidencia de nada.

**Para rehacerlo:** el endpoint documentado es `GET /api/sales/index.json` y acepta `limit`
(máx. 500), `offset`, `plain` y `method` (`all|first|count`). El script está en el servidor en
`/home/anthropic_readonly/verificar_58_en_bims.py` y **pagina mal**: pega a `/sales/` pelado. Antes
de corregirlo conviene correr `sondear_sales_index.py`, que está al lado y compara las formas
—incluido `method=count`, que dice el total de una—. Si `count` viniera 18, el problema no es la
paginación sino el alcance de la credencial, y el crawl completo no sirve.

Los dos scripts traen dos protecciones que **no son opcionales**: desconectan los `FileHandler`
antes de paginar (recorrer ~31.000 ventas quemaría las 4 ventanas de rotación de `bims_api.log`,
como pasó el 23/08 con los contactos) y desconectan Sentry.

---

## 5. Para el Despliegue 2

**Antes de nada:** es el despliegue que cambia el contrato del ingreso. No es uno para dejar a
medias ni para subir un viernes.

1. ⚠️ **Las Tareas 5 y 6 van juntas o no van.** La 5 deja de facturar en línea y la 6 es lo único
   que vacía la cola: subir solo la 5 es dejar de facturar del todo.
2. **Migraciones a aplicar: `0012` y `0013`.** La `0012` es un no-op confirmado (0 filas medidas) y
   la `0013` es un `CREATE TABLE` limpio.
3. ⚠️ **La línea de cron la instala Carlos** (necesita root), y **con el `cd`**:
   `* * * * * /var/www/integrador/process-queue.sh >> /var/log/process-queue.log 2>&1`
4. **Variables nuevas del `.env`, las tres opcionales con default:** `QUEUE_ALERT_THRESHOLD` (10),
   `QUEUE_SILENCE_MINUTES` (10) y `SLACK_WEBHOOK_URL`. Sin webhook no se avisa a nadie y todo lo
   demás funciona igual — pero entonces el Despliegue 2 entra **sin la red de seguridad que lo hace
   seguro**, así que conviene tener el webhook antes.
5. ⚠️ **`SKIP LOCKED` sigue sin cobertura de tests.** Django lo ignora sin error en SQLite, así que
   la exclusión entre workers concurrentes **solo se ejerce en MariaDB**, o sea recién en producción.
6. **Hay dump previo:** `/root/bk/db-pre-expansion.sql.gz` (226 MB, 01/09). Conviene uno nuevo
   antes, porque ese ya tiene dos días de ventas de diferencia.

**Pendiente de revisar, del lado del servidor:** `runretryfaileds.sh` apunta a
`/var/www/integrador.muci.org/backend`, que **no existe**. No se pudo confirmar si el cron lo usa
porque el crontab de root no es legible como `anthropic_readonly`.
