# Handoff sesión del 2026-08-26

> Tres actos: se validó el despliegue de timeouts del día anterior, se implementó entero el
> presupuesto por orden, y se retomó la arquitectura del hub hasta dejar una propuesta lista
> para presentar. **Nada se desplegó.** Producción siguió en `main` (`3b9773c`) todo el día.

## Estado al cierre

| | |
|---|---|
| **Producción** | `main` @ `3b9773c` — **sin cambios en toda la sesión** |
| Rama de trabajo | `feature/presupuesto-por-orden` @ `f07e79d`, pusheada |
| Tests | **152/152** en local (3.12 + Django 6) **y** sobre el stack de producción (3.7.17 + Django 3.2.18) |
| Servicio | último arranque 12:00 UTC — el reinicio programado, anterior a esta sesión |
| Propuesta de donaciones | publicada, **esperando que Carlos la presente** |

---

# ⚠️ LO QUE QUEDÓ SIN COMPROBAR

Esto es lo primero de mañana. Todo lo demás de este documento es contexto.

### 1. El nombre del fee de la propina — **puede haber plata sin facturar**

`services.py:455` solo reconoce la propina si el cargo se llama **exactamente `Tip`**. Otro
nombre → no se factura y **no se loguea nada**.

Lo que se buscó y lo que dio:

| Búsqueda | Muestra | Resultado |
|---|---|---|
| `product_id: 100` en payloads a BIMS | 13 ventas (`bims_api.log*` + `.bak`) | **0** |
| Ventas con más de un ítem | esas 13 | **0** |
| La palabra "propina" en `bims_sync.log` | 5,5 meses (13-mar a 26-ago) | **0** |

**No concluye nada, y por qué:** los tres escenarios —no hay propinas, funcionan, se
pierden— producen el mismo silencio. Una propina exitosa no escribe log; una perdida
tampoco. El código es inobservable justo en el modo de falla que importa.

**Cómo cerrarlo, dos caminos:**

- **Barato y definitivo en días:** una línea que loguee todo fee cuyo nombre no se reconoce.
- **Definitivo ya, necesita root** (el `.env` no lo lee `anthropic_readonly`):

```
ssh root@muci.org "cd /var/www/integrador && /root/.local/share/virtualenvs/integrador-ObaHlHmv/bin/python -c \"
from dotenv import dotenv_values; from woocommerce import API; from collections import Counter
c = dotenv_values('.env')
w = API(url=c['WOOCOMMERCE_URL'], consumer_key=c['WOOCOMMERCE_KEY'], consumer_secret=c['WOOCOMMERCE_SECRET'], version='wc/v3', timeout=30)
r = w.get('orders', params={'per_page':100,'status':'any'}).json()
n = Counter(f['name'] for o in r for f in o.get('fee_lines',[]))
print('ordenes revisadas:', len(r)); print('nombres de fee:', dict(n) or 'NINGUNO')
\""
```

### 2. Smoke test contra el BIMS real — **bloquea el despliegue**

Nunca se corrió. Es lo único que probaría que la tupla `(connect, read)` que introdujo esta
rama viaja bien contra la API de verdad. **Necesita root** (lee el `.env`).

### 3. Django 3.2.18 vs 3.2.25

La suite se validó sobre el `python3.7` **del sistema**, que trae Django **3.2.18**. El venv
de producción tiene **3.2.25** — 7 releases de parche dentro de la misma minor. Prueba
compatibilidad con 3.7 y con la API de 3.2; no prueba el venv exacto. Cerrarlo necesita root.

### 4. ¿Hay `order_id` duplicados en producción?

Bloquea el `unique` que pide el sub-proyecto A: si los hay, **la migración falla**. Hay que
contarlos antes de escribir la migración.

### 5. Que WooCommerce le hable hoy directo al CRM

Afirmado por Carlos (tras `wc-completed`), **nunca verificado** contra Woo ni Krayin. Toda la
propuesta de donaciones se apoya en esto.

---

## 1. El sync nocturno validó el deploy de timeouts

17.304 contactos, **0** presupuestos agotados, **0** timeouts en 38 llamadas. Y 5 órdenes
facturadas sin fallos desde el deploy (Sale 31267 a 31271), la última en ~10 s. **El
presupuesto de 40 s no hay que ajustarlo.**

Confirmado de paso: `bims_api.log.1/.2/.3` los tres fechados 00:03 — el cron quema las 4
ventanas de rotación cada noche. Es la razón de que la muestra del punto 1 sea de 13 ventas.

## 2. Presupuesto por orden, implementado

```
f07e79d chore: script de verificacion sobre el stack real de produccion
1b006d1 docs: handoff (reemplazado por este)
13af3b8 docs: encabezados Task N en el plan
4de77a4 fix: 4 hallazgos del review final de la rama
b2baa85 feat(services): presupuesto por orden y FailedOrder al leer productos
aa263fd docs(woocommerce): documentar el supuesto de concurrencia
c22b9c4 feat(woocommerce): recortar el timeout al restante de la orden
247caf7 feat(bims): respetar el presupuesto de la orden y recortar la conexión
69cadfd feat(deadline): modulo del presupuesto por orden con ContextVar
```

`core/deadline.py` lleva el límite en un `contextvars.ContextVar`, `PRESUPUESTO_ORDEN = 90`,
fijado **solo** en `process_order` con `try/finally`. `sync_bims_contacts` recibe `None` de
`restante()` y **no requirió tocarse**.

**Brecha cerrada que la spec daba por inexistente:** `build_sale_products` no estaba envuelta
en ningún `try/except`, así que un fallo en el `get_product` por ítem se escapaba **sin grabar
`FailedOrder`**. Era un bug preexistente.

**Dos defectos que introdujo el plan**, encontrados por la revisión final y ya arreglados:
`_alternate_base_url` conmutaba el host de forma pegajosa sin mandar request ni loguear; y un
timeout escalar en `requests` aplica a conexión **y** lectura, así que el peor caso llegaba a
~115 s contra los 120 de gunicorn — el margen de 30 s que promete la spec no existía.

**Se decidió NO subir el `--timeout` de gunicorn a 180.** No compra nada (el presupuesto de 90
corta primero; peor caso del diseño ≈105 s) y cuesta: un worker colgado queda atado 50% más.
Si hace falta margen, la palanca es **bajar `PRESUPUESTO_ORDEN`**.

**Herramienta nueva:** `./verificar-en-stack-produccion.sh` corre la suite sobre el stack real
sin tocar producción. Documentado en `CLAUDE.md`.

## 3. Arquitectura del hub, reactivada

Propuesta publicada: **https://claude.ai/code/artifact/1142a0c2-3c6a-4184-9089-c4769e703cd9**
(privada hasta que Carlos la comparta). **Esperando que la presente y decidan.**

Recomienda que las **donaciones manuales entren por Krayin/Fundraising** en vez de por
WooCommerce, porque es la única opción donde el conjunto completo de donaciones existe **por
construcción** y no por acuerdo.

**Corrección importante:** BIMS y el CRM **no deben comunicarse entre sí**. El integrador es
el único que ve los dos lados. Eso disuelve la incógnita original.

**La pregunta que frenó el proyecto el 21/08 está respondida:** a `/sales/` le pega un webhook
de Woo y nadie lee el body — pero Woo **sí cuenta los no-2xx y deshabilita el webhook a las 5
fallas**. `SalesView` devuelve 503 ante cualquier excepción, así que **una caída de BIMS de 5
órdenes corta la facturación en silencio**. Ya le pasó a `Refund order`.

**Decisiones de diseño tomadas** (detalle en la memoria del proyecto): A incluye el async y
responde 202; cola en **MariaDB + cron con `flock`** (Redis existe pero `db0` tiene 270.862
claves de otro sistema); latencia aceptable hasta ~1 min; **extender `FailedOrder` en su
lugar**, sin renombrar hasta que haya backups; alertas de pedido a **Slack** vía Incoming
Webhook, dejando Sentry para problemas de código.

⚠️ **Ojo con Sentry:** `settings.py:14-17` usa `LoggingIntegration(event_level=ERROR)`, así que
**todo `logger.error()` es un evento**. `bims.py` loguea uno por reintento. Separar los canales
exige además **bajar a `warning` los fallos de negocio esperados**, o la falla queda en los dos
lados.

## Lo que sigue, en orden

1. Cerrar los 5 puntos de arriba (el 1 y el 2 necesitan root).
2. Desplegar el presupuesto por orden y mirar el cron de las 00:00 UTC como canario.
3. Que Carlos presente la propuesta de donaciones.
4. **A′** — guardar el `sale_id`: no depende de ninguna decisión pendiente, se puede escribir ya.
5. Spec del sub-proyecto A, ya diseñado.

## Pendientes de antes, sin cambios

- **⚠️ Backups de las bases.** El más grave del proyecto. Sigue sin resolverse.
- **201 órdenes en FAILED** sin reproceso; `runretryfaileds.sh` es código muerto y roto. El
  drenador del sub-proyecto A lo reemplaza y lo vuelve obsoleto.
- **Los logs de BIMS se pierden ~12 h por día** (confirmado otra vez hoy, y es lo que redujo la
  muestra del punto 1 a 13 ventas).
- Spec del proyecto B (Python 3.12 + Django 5.2 LTS).
- Ventana para los 67 parches de terceros y decidir sobre Ubuntu Pro.
- Renombrar la fila del posale 7 a "Caja Fund MuCi" desde el admin.
- Nombres deformados tipo `C L A R I C E`, sin diagnosticar.
- Borrar `feature/gestion-sucursales`, `feature/migracion-api-key` y `feature/timeouts-bims`.

## Menores anotados y no arreglados

Del review final de la rama, todos evaluados y parkeados: el mensaje de agotamiento reporta
`PRESUPUESTO_ORDEN` en vez del presupuesto en efecto si alguien pasa uno custom; un intento
fútil de ~2 s cuando queda una fracción de segundo; dos handlers de `FailedOrder` sin `status`
explícito (`services.py:504,540`); `resolve_pos_and_payments` sin envolver (no puede lanzar
excepciones de presupuesto, pero sí `ValueError`/`KeyError`); el mensaje "excedido por -0.0s"
si `restante` cae exactamente en 0; y dos cosas dormidas de modo sesión —el relogin duplica el
gasto de un intento, y `str(e)` puede arrastrar un `?sid=` a la base—. Producción corre en modo
API Key desde el 24/08.

Fuera de alcance pero anotado: `traces_sample_rate` y `profiles_sample_rate` están al **1.0**
en producción, y el DSN de Sentry está hardcodeado en `settings.py` en vez del `.env`.
