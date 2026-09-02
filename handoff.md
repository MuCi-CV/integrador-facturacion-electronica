# Handoff — sesión del 2026-09-02

**Producción:** `main` @ **`8ad502f`**, desplegado 18:40:31 UTC. Sirviendo tráfico y verificado con
una venta real.
**`main` local y remoto:** **`8996a71`** — un commit más que producción, que es la sincronización de
stock: **sin desplegar y en modo seco**.
**Tests:** **288 OK** en local (3.12 + Django 5.2.17), en el stack de rollback (3.7 + Django 3.2) y
en el **stack real** (3.10.12 + Django 5.2.17).

Tres cosas pasaron hoy: se cerró y desplegó el sub-proyecto A, apareció un hallazgo de negocio que
no buscábamos, y la sincronización de stock quedó diseñada, implementada y mergeada.

---

## 1. ✅ Sub-proyecto A cerrado y en producción

Las 9 tareas hechas, los dos despliegues aplicados. `POST /sales/` ya no factura en línea: persiste y
devuelve **202**, así que **una caída de BIMS no puede volver a apagar el webhook `Venta Entrada`**.
Es el riesgo que le pasó a `Refund order` y lo dejó con `failure_count 6`.

**Verificado con la venta 205290** (Gs 180.000, Pago QR): entró como `PENDING` 19:33:07 y quedó
`COMPLETED` 19:34:23 — **76 segundos**, el minuto del cron más ~16 s de trabajo. `bims_sale_id 31422`,
factura `14610`, `woo_meta_ok=1`, las dos metas en Woo y el `_krayin_lead_id` intacto.

**Las dos pruebas deliberadas salieron como estaban previstas**, y eso es lo que más vale del día:

1. **La reparación de metas drenó el backlog de 76 filas en cuatro pasadas** (`20, 20, 20, 16`) con
   **cero escrituras a WooCommerce**. Se predijo `0 anotada(s), 20 ya estaba(n)` antes de correrlo y
   salió textual. Con el PUT directo del plan habrían sido 76 escrituras inútiles y 76
   `order.updated` despertando al bot de WhatsApp por órdenes viejas.
2. **La alerta se probó a propósito** con la orden inexistente `999999999`: quedó `FAILED` con 5
   intentos y el mensaje llegó al canal. **La fila en `core_alertthrottle` es la prueba de que el
   POST salió**, porque la marca se escribe DESPUÉS de que Slack contesta.

**Lo que hay que recordar de las Tareas 7 y 8** (el detalle está en `progreso.md`):

- El **throttle de alertas vive en la base**, no en `django.core.cache`: no hay `CACHES`
  configurado, `LocMemCache` es por proceso, y el worker es un proceso nuevo cada minuto. El
  `cache.add` del plan habría mandado 60 mensajes por hora durante una caída de BIMS.
- ⚠️ **Ningún disparador detecta que el cron esté muerto.** El plan se lo atribuía a uno, pero es
  imposible desde adentro del cron. Hace falta un **latido externo** y quedó fuera de alcance.
- ⚠️ El plan de la Tarea 9 decía **"sin `migrate`" y era FALSO**: faltaban la `0012` y la `0013`, y
  sin ellas el worker revienta al primer aviso. Runbook corregido en
  `docs/2026-09-02-despliegue-2-runbook.md`, que también documenta la trampa del rollback (las filas
  en `PENDING`/`PROCESSING` quedan huérfanas con el código viejo).

**Cabos abiertos, menores:** `/var/log/process-queue.log` sin `logrotate`; `AlertThrottle` no está en
el admin (serviría para silenciar un aviso a mano); y `settings.py:145-147` duplica cada línea en
stdout, que es preexistente.

---

## 2. Hallazgo de negocio: 58 pedidos que nunca llegaron al integrador

`wc-completed`, monto > 0, sin fila, desde el 2025-10-13: **58 pedidos, Gs 7.268.027**. Son dos cosas
distintas y **no conviene mezclarlas**:

| | pedidos | monto | qué es |
|---|---|---|---|
| Cortesía | 20 | 2.878.027 | **no es falla**: hasta el 27/08 el código las descartaba a propósito |
| Pago cobrado | **38** | **4.390.000** | la pérdida silenciosa real |

Los 58 están en `wc-completed`, **0 cancelados y 0 con devolución**. El más reciente es del
**2026-08-26**: desde el 27/08 no se perdió ninguno.

**El dato que más pesa:** el 192578 y el 192584 tuvieron 10 y 2 cambios de estado por edición masiva
entre el 14 y el 16/07 — alguien forzando el reenvío a mano. El reenvío **llegó** las dos veces,
resolvió el contacto y **se cortó sin error y sin dejar fila**, mientras órdenes del mismo minuto
completaban normal. Es el 503-sin-persistir de `SalesView`, con nombre y número de orden.

**Planilla ya enviada a Finanzas.** La columna "Facturado en BIMS" dice **"Pendiente de verificar"**,
y eso es a propósito: el chequeo automático **quedó inválido** porque el listado devolvió **18 ventas
de más de 31.000**. ⚠️ **No tomar esa corrida como evidencia de nada.**

**Para rehacerlo:** el endpoint es `GET /api/sales/index.json` con `limit` (máx. 500), `offset`,
`plain` y `method`. Los scripts están en `/home/anthropic_readonly/` del servidor;
`verificar_58_en_bims.py` **pagina mal** (pega a `/sales/` pelado). Antes de corregirlo, correr
`sondear_sales_index.py`, que está al lado: si `method=count` diera 18, el problema no es la
paginación sino el **alcance de la credencial**, y el crawl completo no sirve.

Los scripts traen dos protecciones que **no son opcionales**: desconectan los `FileHandler` antes de
paginar (recorrer ~31.000 ventas quemaría las 4 ventanas de `bims_api.log`, como pasó el 23/08 con
los contactos) y desconectan Sentry.

---

## 3. Sincronización de stock BIMS → WooCommerce: implementada, sin desplegar

Se empezó **verificando si la tarea ya estaba hecha. No lo estaba:** tres implementaciones y ninguna
corriendo. La prueba dura es que la tabla `wpzv_bimsc_stocks` tiene **0 filas** desde que se creó el
2025-10-13, y ningún producto de Woo tiene el meta `_bims_sync`.

- **Spec:** `docs/superpowers/specs/2026-09-02-sincronizacion-stock-bims-design.md`
- **Plan:** `docs/superpowers/plans/2026-09-02-sincronizacion-stock-bims.md` — 10 tareas, 51 pasos, cerrados
- **Los datos duros de la API:** memoria `reference_topologia_stock_bims`

**Todo se midió contra la API viva.** Los hallazgos que definieron el diseño:

| hallazgo | consecuencia |
|---|---|
| BIMS **no tiene webhook de salida** ni filtro "modificado desde" | es **sondeo**, no evento. El pedido hablaba de un evento; esto es lo más cercano que la API permite |
| el número vendible es **`total`**, no `total2` | `Product.availability` que calcula BIMS es la suma de `total`; `total2` trae negativos proporcionales a la venta |
| todo el stock vive en **San Cosmos (6) y GIFTSHOP MÓVIL (7)** | **Casa Matriz tiene 0** en los 427 inventariables: la hipótesis inicial quedó descartada con datos |
| el **SKU de Woo ES el id de producto de BIMS** | ningún producto de BIMS tiene `code` (0 de 427), así que el puntero vive del lado de Woo |
| `availabilities/index.json` **pagina sin orden estable** | descartado: un barrido por ahí puede perderse un producto entero |
| `products?v_stock=1` con `limit=500` **da timeout** | se pagina de a 100, con un test que impide subirlo |

**Tres guardas, todas con test:** una lectura fallida de BIMS **no escribe nada**; un barrido que
apagaría más de 5 productos aborta y avisa a Slack; y el **modo seco es el default**.

⚠️ **La secuencia de despliegue tiene un paso que no se saltea:**

1. Subir el código con el flag apagado y **sin** cron. No cambia nada en la web: no hay migraciones
   y `STOCK_SYNC_ENABLED=false`.
2. **Correr `manage.py sync_stock` a mano, EN SECO, y leer la lista completa.** Es la única
   oportunidad de confirmar que los depósitos `6,7` son los correctos antes de que lo vea un
   cliente, y de ver cuántas variaciones se descartan por SKU ambiguo (medido entre 16 y 32; el
   barrido lo va a contar de verdad).
3. Con esa lista aprobada, instalar el cron
   (`*/15 * * * * root /var/www/integrador/sync-stock.sh`) y **recién después**
   `STOCK_SYNC_ENABLED=true`.

**Se espera que el primer barrido ENCIENDA productos, no que apague.** Hoy la web muestra agotado lo
que sí hay: `JUGUETE CARTAS INFANTILES SC` dice 16 y tiene **71**.

**El veredicto de la comparación** (código rescatado vs `bimsc`): repartido, y en la llave de vínculo
**no gana ninguno** — `_bims_id` cubre 186 productos con 7 ambiguos, y el filtro por meta del
rescatado **no existe en la REST de Woo**. Gana el **SKU**, que es lo que el integrador ya usa para
facturar. Sobreviven ~40 líneas del código rescatado.

**De paso se arregló un bug real:** `build_sale_products` hacía `int(sku)` y reventaba con
`invalid literal for int()` ante un SKU de baja — hay una fila así en producción. Ahora levanta
`SkuDadoDeBaja` con mensaje legible. **El comportamiento no cambió** (la orden sigue fallando, como
pidió Carlos) porque un SKU no numérico **es** la marca de producto dado de baja: al darlo de baja,
el `7` pasa a `7-1`, `7-5`, `7-19` según cuántas veces.

⚠️ **No mergear `feature-sync`** para traer el original: ese commit (`c6a87dd`) mete además
`SimpleForgotPasswordView` / `ForgotPasswordView`, que **generan una password temporal y la devuelven
en el cuerpo de la respuesta HTTP**. `main` hoy no lo tiene.

---

## 4. Para mañana

**Lo primero, si se decide avanzar:** la secuencia de arriba, empezando por el barrido en seco. No
hay apuro — el código está en `main`, verificado en los tres stacks, y apagado.

### 🔴 Seguridad, y no es menor

- Las opciones del plugin `bimsc` guardan **usuario y contraseña de BIMS en texto plano** en
  `wpzv_options`, y el plugin está inactivo desde 2025. **Borrar esas opciones y rotar la
  credencial.**
- La **contraseña del usuario MySQL `anthropic_readonly`** se pegó en el chat de esta sesión.
  Conviene rotarla, o dejarla en un `~/.my.cnf` con `chmod 600` para no tener que pegarla nunca más.
- El reporte a BIMS por la fuga de credencial **sigue sin enviarse**
  (`docs/reportes/2026-08-27-reporte-a-bims.md`).

### Decisiones de negocio pendientes

- Qué se hace con los **38 pedidos con pago cobrado** (Gs 4.390.000) que nunca llegaron. Finanzas
  tiene la planilla.
- Si las **149 cortesías históricas con precio** se facturan retroactivamente. 20 están en el hueco
  de los 58.
- Los **12 padres con variaciones de SKU ambiguo** quedaron fuera de alcance por decisión de Carlos.
  El barrido los reporta en cada pasada, así que la lista está disponible el día que se quiera
  actuar.

### Cosas del servidor que siguen sin resolver

- ⚠️ **`runretryfaileds.sh` apunta a `/var/www/integrador.muci.org/backend`, que no existe.** No se
  pudo confirmar si el cron lo usa: el crontab de root no es legible como `anthropic_readonly`.
- ⚠️ **El webhook `Refund order` sigue deshabilitado** (`failure_count 6`) y apunta a
  `muci-integrador.staging.girolabs.cloud/refunds/`, una URL de **staging** — nunca estuvo apuntando
  a producción. Mientras siga así, **toda cancelación se resuelve a mano en los dos sistemas**. BIMS
  tiene `POST /api/sales/cancel/{id}.json` para cuando se decida cerrarlo.
- `logrotate` para `/var/log/process-queue.log`, y el mismo cuidado con `/var/log/sync-stock.log`
  cuando se instale.

### Estado de las ramas

`feature/hub-ingreso-cola` y `feature/sincronizacion-stock-bims` quedan vivas, las dos
**completamente mergeadas** a `main`. Se pueden borrar con `git branch -d` sin perder nada; se
dejaron porque es la práctica del proyecto.

⚠️ Y un detalle que engaña: hay **dos remotos al mismo repo**, `github` y `origin`, y la ref local de
`origin` está más atrasada. El upstream real es `github`, así que un `git log origin/…` da una cuenta
distinta y equivocada.
