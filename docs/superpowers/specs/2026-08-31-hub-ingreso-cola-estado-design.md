# Sub-proyecto A — Ingreso, cola y estado por rama

**Fecha:** 2026-08-31
**Estado:** diseño aprobado, pendiente de plan de implementación
**Depende de:** A′ (correlación orden↔factura), ya desplegado el 2026-08-28
**Habilita:** C (rama CRM) y F (entrada de donaciones desde Krayin)

---

## 1. El problema

El integrador hace todo el trabajo de una orden **dentro del request HTTP** del webhook de
WooCommerce, y no persiste nada hasta que el trabajo ya empezó. De ahí salen tres problemas
distintos que comparten la misma causa.

### 1.1 WooCommerce puede apagarnos la facturación

`SalesView` devuelve **503 ante cualquier excepción** (`views.py`). Woo **cuenta los no-2xx y
deshabilita el webhook a las 5 fallas seguidas**. Una caída de BIMS de cinco órdenes apaga
`Venta Entrada` y **la facturación se corta en silencio**.

No es hipotético: ya le pasó al webhook `Refund order`, que quedó deshabilitado con
`failure_count 6` y apuntando a un host que ya no existe.

### 1.2 Una orden lenta se pierde entera

El unit de gunicorn corre con `--timeout 120`. Si una orden pasa ese límite, gunicorn **mata al
worker por señal**, y un proceso matado por señal **no ejecuta el `except`** que graba el
`FailedOrder`. La orden desaparece: sin factura y sin registro de que existió.

### 1.3 En WooCommerce, la ausencia de dato no significa nada

`update_order_meta` se llama en **un solo lugar**: la rama de éxito (`services.py:659`). Todas las
ramas de fallo escriben `FailedOrder` y relanzan, y las de "no corresponde facturar" retornan sin
dejar fila alguna.

Resultado: que una orden **no** tenga `_bims_sale_id` hoy significa cinco cosas distintas.

| la meta no está porque… | qué queda registrado |
|---|---|
| Falló al facturar | fila `FailedOrder` en FAILED |
| No correspondía facturar (monto 0, descuento 100%, productos en 0) | **nada** |
| Todavía no se procesó | **nada** |
| Es anterior al 2026-08-28 | la feature no existía |
| Facturó, pero la escritura de la meta falló | **nada** (caso real: orden 204000) |

Un dato positivo es confiable; su ausencia no informa. Y ése era justamente el objetivo.

---

## 2. Objetivo

Que **toda** transacción que entra al integrador quede persistida **antes** de procesarse, con un
estado explícito y consultable por cada rama de salida, y que el ingreso no pueda hacer que
WooCommerce nos apague el webhook.

### No-objetivos (fuera de alcance, deliberadamente)

- **La rama CRM.** No se escribe a Krayin. Es el sub-proyecto **C**.
- **La entrada de donaciones desde Krayin.** Es el sub-proyecto **F**. Esta spec **generaliza el
  esquema para que quepa**, pero no construye la entrada.
- **Renombrar la clase `FailedOrder`.** Es cosmético, tiene el blast radius más grande y no
  bloquea nada. Va como cambio propio.
- **Paralelismo en el worker.** Ver §7.1: se decide con datos, no con suposiciones.

---

## 3. Decisiones ya tomadas y por qué

Las cinco primeras son del 2026-08-26 y esta spec las hereda sin re-litigarlas. Las dos últimas se
tomaron al escribir este diseño.

| Decisión | Elegido | Razón |
|---|---|---|
| Alcance | **incluye el async** | la tabla de "persistir al ingresar" **es** la cola; diseñarla dos veces no tiene sentido |
| Mecanismo de cola | **MariaDB + cron con `flock`** | Redis existe pero **no es nuestro** (`db0` con 270.862 claves de otro sistema) y los caches se vacían. MariaDB ya es el almacén propio, es transaccional y se ve desde el admin |
| Latencia aceptable | **~1 minuto** | permite cron en vez de un worker dedicado; calca cómo ya corre `sync_bims_contacts` |
| Modelo de estado | **extender `FailedOrder` en su lugar** | ya *es* de facto la tabla de estado. Una tabla nueva en paralelo daría **dos fuentes de verdad sobre si una orden se facturó**, que es lo peor posible para estado fiscal |
| Canal de alerta | **Slack (Incoming Webhook)** | Sentry queda para problemas de código |
| **Identidad de la fila** | **generalizar ahora** (`referencia_externa` + `origen`) | la topología de **dos orígenes** ya está decidida y publicada (26/08), así que una fila sin `order_id` es certeza, no hipótesis. Con backups disponibles desde el 28/08, migrar la tabla fiscal **una vez** en vez de dos |
| **Backoff por rama** | **solo la rama BIMS** | ver §5.2 |

### Infraestructura verificada (2026-08-26)

- **MariaDB 12.3.2** → `SELECT … FOR UPDATE SKIP LOCKED` disponible (requiere ≥10.6).
- **`flock` en `/usr/bin/flock`**.
- No hay celery/rq; el único servicio del integrador es `mucintegrador.service`.

---

## 4. El contrato de ingreso

`SalesView` pasa a hacer **solo dos cosas**: validar que llegó una referencia, y persistirla.

| situación | respuesta |
|---|---|
| Request válido | **`202 Accepted`**, en milisegundos |
| Falta la referencia en el body | `400 Bad Request` |

**El único no-2xx que queda depende de nosotros, no de terceros.** BIMS puede estar caído una
semana entera sin que Woo apague el webhook.

### Idempotencia del ingreso

`update_or_create` sobre `(origen, referencia_externa)`. Un reintento del webhook de Woo **no**
duplica la fila. Y qué pasa con una re-entrega depende del estado actual:

| estado actual | qué hace una re-entrega |
|---|---|
| `PENDIENTE` / `EN_PROCESO` | **nada** — ya está encolada, no se reencola ni se reinicia |
| `FAILED` | **vuelve a `PENDIENTE`** — es la forma natural de pedir un reproceso |
| `COMPLETED` | **nada** — ya se facturó; reprocesar es riesgo sin beneficio |
| `NO_APLICA` | **vuelve a `PENDIENTE`** — la orden pudo haber cambiado de monto |

### El costo que hay que asumir

**Con 202 siempre, una integración rota deja de gritar.** Todo se encola en silencio. Por eso la
alerta (§6) es parte del alcance de A y no un extra: sin ella estaríamos cambiando una falla
ruidosa por una invisible.

---

## 5. Modelo de datos

### 5.1 Estados

Hoy existen dos y **8.588 filas** los usan (201 FAILED, 8.387 COMPLETED). **Los valores numéricos
existentes se conservan**, así que no hay migración de datos sobre el eje principal:

| estado | valor | significado |
|---|---|---|
| `FAILED` | 1 | *(existente)* agotó sus reintentos |
| `COMPLETED` | 2 | *(existente)* facturado en BIMS |
| `PENDIENTE` | 3 | persistida, sin procesar |
| `EN_PROCESO` | 4 | tomada por un worker |
| `PAUSADA` | 5 | esperando algo externo |
| `NO_APLICA` | 6 | monto 0, descuento 100%, o todos los productos en 0 |

Dos de los nuevos cierran agujeros concretos:

- **`PAUSADA`** reemplaza un canal de estado improvisado: hoy `sync_bims_contacts.py:63` filtra por
  `message__startswith("Pausada: Esperando")`. **Reformular ese texto rompe el comando en
  silencio.** Un estado real no se rompe al editar un mensaje.
- **`NO_APLICA`** es lo que desambigua el punto §1.3: esas órdenes hoy **no dejan fila ninguna**.

### 5.2 Campos

| campo | cambio | notas |
|---|---|---|
| `referencia_externa` | **reemplaza `order_id`** | `CharField`. Texto porque el CRM no usa enteros |
| `origen` | **nuevo** | `woo` en todo A. `crm` lo agrega F |
| `unique` | **nuevo** | compuesto sobre `(origen, referencia_externa)` |
| `intentos_bims` | nuevo | contador para el backoff |
| `proximo_intento_bims` | nuevo | `DateTimeField` nullable; el worker no toca filas con futuro |
| `meta_woo_ok` | nuevo | `BooleanField`; ver abajo |
| `tomada_en` | nuevo | `DateTimeField` nullable, para el reaper |
| `status`, `message`, `bims_sale_id`, `bims_invoice_number`, `created_at`, `updated_at` | sin cambios | |

**Por qué la rama de Woo no tiene backoff propio.** Las dos ramas tienen naturalezas distintas: la
de BIMS falla por caídas y lentitud y merece cronograma; la de anotar la meta en Woo es **una
llamada barata e idempotente**, así que alcanza con reintentarla en cada pasada mientras
`meta_woo_ok` esté en falso y ya exista `bims_sale_id`. Darle backoff propio a cada rama significa
tres columnas por rama, o **nueve columnas** cuando entre el CRM. La complejidad la paga solo la
rama que la necesita.

**Esto además repara el caso 204000 de raíz**, no solo lo instrumenta: un fallo al anotar deja una
fila reintentable que se auto-repara en la pasada siguiente, en vez de un `WARNING` que se pierde.

### 5.3 Migración

1. **Esquema:** renombrar `order_id` → `referencia_externa` y cambiar a `CharField`; agregar
   `origen`, los cuatro campos nuevos, y el `unique` compuesto.
2. **Datos, en la misma migración:**
   - todas las filas existentes → `origen='woo'`;
   - `referencia_externa` = `str(order_id)`;
   - `meta_woo_ok = True` para las `COMPLETED` **anteriores** al 2026-08-28 (la feature de metas no
     existía; marcarlas en falso dispararía 8.000 reintentos inútiles contra Woo);
   - `meta_woo_ok = False` para las `COMPLETED` **del 2026-08-28 en adelante**: de esas no sabemos
     desde nuestra base si la meta llegó, y son una decena. El worker las verifica y repara en la
     primera pasada, que es exactamente el comportamiento deseado — **la orden 204000 es una de
     ellas**;
   - las `FAILED` cuyo `message` empieza con `"Pausada: Esperando"` → `PAUSADA`.
3. **`unique` seguro:** verificado el 2026-08-27 — 8.588 filas, 8.588 `order_id` distintos, **0
   duplicados**.

⚠️ **El punto 2 es una migración de datos sobre la tabla de estado fiscal.** Requiere backup
inmediatamente antes (`backup-bases.sh`) y debe ser reversible.

### 5.4 Consumidores a actualizar

`admin.py` (dos vistas custom y una acción), `services.py`, `retryfaileds.py`,
`sync_bims_contacts.py` y **23 referencias en `tests.py`**.

`sync_bims_contacts.py:63` pasa a filtrar por `status=PAUSADA`.
`core/management/commands/retryfaileds.py` —que invoca `runretryfaileds.sh`— queda en gran parte
**absorbido** por el worker: la implementación debe decidir explícitamente si sigue teniendo razón
de existir o si se retira junto con su script.

---

## 6. El worker y la alerta

### 6.1 El worker

Management command por cron **cada minuto**, envuelto en `flock` para que dos corridas no se
solapen. Calca la forma en que ya corre `sync_bims_contacts`.

Por corrida:

1. Toma filas `PENDIENTE` con `proximo_intento_bims` vencido o nulo, usando
   `SELECT … FOR UPDATE SKIP LOCKED`; las marca `EN_PROCESO` con `tomada_en`.
2. Procesa cada una con la lógica de `process_order` que ya existe.
3. Reintenta la rama de Woo en las filas con `bims_sale_id` y `meta_woo_ok = False`.
4. **Reaper:** las `EN_PROCESO` con `tomada_en` más viejo que el umbral vuelven a `PENDIENTE`.

**El reaper es seguro porque BIMS deduplica por `_id`**, así que reprocesar no emite una segunda
factura. Sin esa garantía, un reaper sobre datos fiscales sería inaceptable.

### 6.2 La alerta

Slack por Incoming Webhook: `SLACK_WEBHOOK_URL` en el `.env`, y la línea correspondiente en
`.env.example`. Tres disparadores, **con throttling** (una caída de BIMS no debe mandar 200
mensajes):

1. **Cola por encima de un umbral** — configurable, arranca conservador.
2. **Una orden agotó sus reintentos de BIMS.**
3. **Nada se procesó en X minutos** — atrapa que el cron esté muerto, que es el único caso que los
   otros dos no ven.

### 6.3 Corrección necesaria en el logging

**No alcanza con sumar Slack.** `settings.py` usa `LoggingIntegration(event_level=logging.ERROR)`,
así que **cada `logger.error()` es un evento de Sentry**, y `bims.py` loguea un error **por cada
reintento**: una orden lenta genera 3-4 eventos indistinguibles de un bug real.

Hay que **bajar a `logger.warning` los fallos de negocio esperados**. Si no, la misma falla grita
en los dos canales y ninguno queda confiable.

### 6.4 Parámetros configurables

La spec no fija estos valores en el código: van a `settings` (con default) para poder ajustarlos
sin desplegar. Los valores de arranque son propuestas conservadoras, no resultados de medición.

| parámetro | arranque | por qué |
|---|---|---|
| Intentos de BIMS antes de `FAILED` | **5** | cubre una caída corta sin retener una orden rota para siempre |
| Backoff entre intentos | **1, 5, 15, 60 min** | el primer reintento rápido atrapa el error transitorio; los siguientes esperan a que alguien arregle BIMS |
| Umbral de cola para alertar | **10 filas `PENDIENTE`** | ~2 corridas de atraso. Deliberadamente bajo: la primera semana **mide el pico** (§7.1) |
| Silencio antes de alertar | **10 min sin procesar nada** | dos veces el intervalo del cron más margen |
| Antigüedad para el reaper | **10 min en `EN_PROCESO`** | muy por encima de los ~14 s de una orden normal |
| Throttle de Slack | **1 mensaje cada 15 min por tipo** | una caída de BIMS no debe mandar 200 mensajes |
| Reintentos de la meta en Woo | **20, y después deja de intentar** | evita que una orden rota reintente para siempre; queda visible en el admin |

---

## 7. Riesgos

### 7.1 Backlog en picos

El worker corre cada minuto y una orden tarda **~10-14 segundos**. Con volumen normal sobra, pero
en una tanda (cierre de un evento) una corrida puede no vaciar la cola y la siguiente arranca con
backlog, rompiendo la latencia de ~1 minuto.

**No tenemos el dato del pico real.** Decisión tomada: **no** paralelizar todavía; alertar por
tamaño de cola.

Vale notar que **esa alerta es también el instrumento que produce el dato que falta**. El primer
despliegue mide el pico: si nunca dispara, el diseño simple alcanzaba; si dispara, sabemos cuánto y
recién ahí se discute paralelismo con números. Por eso el umbral arranca **configurable y
conservador**.

Si se paraleliza en el futuro, ojo: `wc_api` es un **singleton de módulo con timeout mutable de
instancia** que hoy asume `--threads 1` (documentado en `woocommerce.py`). Paralelizar sin
resolver eso corrompe los timeouts entre órdenes.

### 7.2 La migración de datos toca la tabla fiscal

Mitigación: backup inmediatamente antes, migración reversible, y verificar los conteos por estado
antes y después.

### 7.3 El 202 esconde las fallas

Mitigado por §6.2. **Si la alerta no se implementa, A empeora el sistema en vez de mejorarlo.**

---

## 8. Testing

- **Ingreso:** 202 con referencia válida; 400 sin ella; la fila queda `PENDIENTE`; **BIMS nunca se
  llama durante el request**.
- **Idempotencia:** las cuatro filas de la tabla de §4, una por estado.
- **Worker:** toma y marca `EN_PROCESO`; procesa a `COMPLETED` con `sale_id` y factura; reintenta
  con backoff y llega a `FAILED` tras N; el reaper recupera una fila colgada.
- **Rama Woo:** una fila con `bims_sale_id` y `meta_woo_ok=False` se repara en la pasada siguiente
  (**es el caso 204000**).
- **Estados nuevos:** monto 0 → `NO_APLICA` con fila creada; `sync_bims_contacts` lee `PAUSADA`.
- **Alerta:** dispara por los tres motivos; el throttling evita la repetición.
- **Migración:** sobre un fixture que reproduzca los tres casos del §5.3 punto 2.

Mocks con `responses`/`unittest.mock`, sin tráfico real a BIMS ni a Woo. Verde en local **y** sobre
el stack real (Python 3.10.12 + Django 5.2.17) antes de desplegar.

---

## 9. Criterios de éxito

1. Cinco fallas seguidas de BIMS **no** deshabilitan el webhook de Woo.
2. Toda transacción que entra queda persistida antes de procesarse; matar el proceso a mitad no
   pierde la orden.
3. Cada orden tiene un estado explícito: la ausencia de `_bims_sale_id` en Woo deja de tener cinco
   significados.
4. Un fallo al anotar la meta se repara solo en la pasada siguiente.
5. Una caída de BIMS produce **una** alerta en Slack, no silencio ni 200 mensajes.
6. El esquema admite `origen='crm'` sin migrar la tabla fiscal otra vez.
