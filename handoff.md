# Handoff sesión del 2026-08-27

> Se cerraron los 5 puntos que el handoff anterior dejaba sin comprobar, se **desplegaron dos
> cosas** y se verificaron las dos con tráfico real. Un pedido cambió de forma por completo a mitad
> de camino, y apareció una **fuga de credencial** que no tiene que ver con nada de lo anterior.

## Estado al cierre

| | |
|---|---|
| **Producción** | `main` @ **`12cf312`** — dos despliegues hoy, los dos verificados en vivo |
| Servicio | último arranque **19:26:54 UTC** (el del segundo despliegue) |
| Tests | **155/155** en local (3.12 + Django 6) y sobre el **venv exacto** de producción (3.7.17 + Django 3.2.25) |
| Ramas sin mergear | `chore/verificar-stack-palancas` (tooling, ver abajo) |

---

# ⚠️ LO PRIMERO DE MAÑANA

### 1. La fuga de credencial en `bims_api.log`

La respuesta de `POST /sales/` de BIMS incluye `Agency.tae_password` **en texto plano** (más
`tae_username`). El integrador loguea el body crudo, así que **cada venta exitosa escribe esa
credencial en disco**, replicada por la rotación en `.1/.2/.3` y `.bak`.

Son **dos problemas separados**: que BIMS lo mande (hay que reportarlo, es su credencial) y que
nosotros lo escribamos sin filtrar (nuestro arreglo: redactar `password|secret|token|api_key` antes
de loguear). El log **no está en git** desde `e53b849`, así que no se filtró al repo.

**El reporte ya está escrito:** `docs/reportes/2026-08-27-reporte-a-bims.md`. Falta **enviarlo**.
Está en dos partes a propósito — la de seguridad conviene mandarla sola.

### 2. El cron de las 00:00 UTC — canario del presupuesto por orden

`sync_bims_contacts` hace 38 llamadas secuenciales y **no debe** tener presupuesto de orden: tiene
que recibir `None` de `restante()`. Se verificó fuera de contexto en el smoke test, pero la corrida
completa de 17.300 contactos todavía no pasó con este código. Si el sync termina sin presupuestos
agotados, el despliegue queda validado del todo.

---

## 1. Los 5 puntos sin comprobar, cerrados

| # | Punto | Resultado |
|---|---|---|
| 1 | Nombre del fee de la propina | **Sin plata perdida.** En todo el histórico hay dos nombres: `Tip` 699 veces (551 en `completed`, desde 2023-10-08) y `Giftcard (777#gcn)` 3 veces —todas **negativas** y en órdenes **canceladas**, o sea que nunca llegaron. El riesgo real es otro: el nombre sale de `esc_html__('Tip', 'wpslash-tipping')`, **cadena traducible**. Un `.mo` lo vuelve "Propina" y las propinas dejan de facturarse en silencio |
| 2 | Smoke test contra el BIMS real | **Pasa.** `login` 2,19 s, `get_posales` OK con la tupla `(connect, read)`, y con presupuesto agotado corta en **0,0 s** en vez de colgarse |
| 3 | Venv exacto | **155/155 sobre Django 3.2.25** |
| 4 | `order_id` duplicados | **No hay.** 8588 filas, 8588 distintos → la migración del `unique` del sub-proyecto A es segura |
| 5 | ¿Woo le habla al CRM? | **Sí, por plugin.** `woocommerce-krayin-crm`, en `woocommerce_order_status_changed`, vía Action Scheduler. 134 leads en un día, todos 200 |

Del punto 5 salió algo útil: **la correlación pedido↔lead ya existe** (`_krayin_lead_id` como meta
de la orden), así que el eslabón que falta es solo **orden↔factura de BIMS**, que es el
sub-proyecto A′.

## 2. Presupuesto por orden — DESPLEGADO 13:41:02 UTC

Fast-forward `3b9773c..11d4780`, `migrate` no-op, restart. Sin variables nuevas en el `.env`.

**Verificado en vivo:** la orden 201914 entró a los 27 s del reinicio y la **201916 facturó completa
(Sale 31280) en ~9,7 s**. Cero presupuestos agotados, cero errores. Cierra el hallazgo 1 de los
timeouts, que era la condición para discutir sacar el reinicio cada 6 h.

## 3. Cortesía — DESPLEGADO 19:26:54 UTC y verificado con una venta real

**El pedido cambió de forma.** Arrancó como *"que el merch con precio 0 llegue para descontar
inventario"*; tras hablar con finanzas resultó ser otra cosa: que una venta de caja con método
**Cortesía** llegue **con el precio original** y sea **rastreable**.

**Hallazgo central:** FooEvents **no tiene** un método "Cortesía". La caja usa el slot
`fooeventspos_direct_bank_transfer` reetiquetado — **1036 órdenes desde 2023-10-11, ninguna llegó
nunca**. La transferencia bancaria de verdad es `cash_on_delivery` (26). Confundirlos haría figurar
una cortesía como cobrada.

**Verificación end-to-end:** orden Woo **202707 → BIMS Sale 31301**, `payment_method_id: 43`,
producto *Tazas Pequeñas SC* a **35.000 = su `sell_price`**, `invoice_number 12000`, certificada ante
la SET (`eis_response: "(0300) Lote recibido con éxito"`). La caja después anuló pedido y factura
**a mano en los dos sistemas**, que es lo correcto: el webhook `Refund order` está **deshabilitado**
y apunta a un `staging.girolabs.cloud` inexistente, así que una cancelación en Woo **no se propaga**.

**Alcance acordado:** solo las cortesías que **ya traen precio** (149 de 961 `completed`). Las **812**
con las líneas en 0 quedan afuera; las frena el chequeo de `total == 0`, que corre **antes** que el
de método de pago. Hay un test que lo fija para que no se cuelen.

**De paso, el fallback dejó de ser silencioso:** cualquier `opmk` desconocido se facturaba como
"En línea" (28) sin loguear nada — el mismo bug que tuvo Cortesía 3 años. Ahora avisa con `WARNING`.

## 4. El spike del inventario, y lo que respondió de rebote

Quedó reemplazado por Cortesía, pero dejó cosas útiles:

- **La fuente de verdad de la API de BIMS es `/home/vallory/IA/bims/bims_docs/docs/openapi.json`**
  (340 paths, 119 esquemas), no `ayuda.bims.app` (3 endpoints). Y
  `/home/vallory/code/plugin-factura-electronica/wc-bims-integrador/bims1.apib` tiene los payloads
  concretos que el openapi deja genéricos.
- **`send_invoice` resuelto:** `POST /api/sales/send/{id}.json` es *"envía el documento **al
  cliente**"* — entrega, no emisión. Nuestro `bims.py:618` está mal nombrado y es código muerto.
  La certificación es **automática al guardar**.
- **Una venta facturada normal SÍ descuenta inventario** (`Sale.stock: true`), confirmado en la venta
  31301. Sigue sin saberse si una con `billed: false` lo hace.
- **`stock_uses` (órdenes de uso interno) es solo lectura**: existe `index`, no `add`. Es el pedido
  2.1 del reporte a BIMS.

## 5. Django 5.2 LTS — objetivo marcado por Carlos

**Producción corre Django 3.2, sin soporte desde abril de 2024.** 4.2 LTS también venció (abril
2026), así que **5.2 es el único destino LTS vigente**. Restricción que ordena el trabajo: **5.2 pide
Python ≥ 3.10 y producción tiene 3.7.17**, así que son dos saltos y el intérprete va primero. Es el
"proyecto B", pero es deuda de seguridad, no modernización. Y **sin backups no es reversible**.

---

## Cabos sueltos

- **`chore/verificar-stack-palancas` sin mergear ni pushear.** Hoy costó un error real: verifiqué
  contra Django 3.2.18 creyendo que era 3.2.25, porque la palanca `PYTHON=` no está en `main` y el
  script viejo **ignora la variable en silencio**. Mientras viva solo en esa rama, cualquier
  verificación desde otra rama usa el intérprete equivocado sin avisar.
- **`payment_method_title` quedó como parámetro muerto** en `resolve_pos_and_payments`
  (`services.py:91`, pasado desde `:541`). Decisión explícita de Carlos: sacarlo toca la firma y 21
  call sites de tests. Documentado en el commit.
- **La tilde de `Cortesia` en BIMS** (id 43): Carlos la va a corregir. **Tiene que ser un rename
  sobre el 43**, no un método nuevo, o el mapeo del código apunta al equivocado.
- **Sentry:** cualquier script de diagnóstico con los settings de producción reporta como si fuera
  la app — pasó hoy con un `KeyError` de un sondeo mío. La línea para evitarlo:
  `sentry_sdk.get_global_scope().set_client(None)` después de `django.setup()`. Y siguen en 1.0
  `traces_sample_rate` y `profiles_sample_rate`, con el DSN hardcodeado.
- **`scp` a producción está bloqueado** por el classifier; `git archive | ssh` y `tar -c | ssh` sí
  pasan. Y el acceso root **necesita** `-o IdentitiesOnly=yes`.

## Pendientes de antes, sin cambios

- **⚠️ Backups de las bases.** El más grave del proyecto, y ahora bloquea el upgrade de Python.
- **201 órdenes en FAILED** sin reproceso; `runretryfaileds.sh` es código muerto y roto.
- Los logs de BIMS se pierden ~12 h por día por la rotación del cron nocturno.
- Ventana para los 67 parches de terceros.
- Propuesta de donaciones publicada, **esperando que Carlos la presente**.
- **A′** — guardar el `sale_id`: no depende de ninguna decisión pendiente.
