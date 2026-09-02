# Sincronización de stock BIMS → WooCommerce — Diseño

**Fecha:** 2026-09-02
**Rama:** `feature/sincronizacion-stock-bims`
**Estado:** implementado en la rama (288 tests verdes en local y en el stack de rollback), pendiente del primer barrido en seco

## El problema

WooCommerce muestra agotado lo que sí hay. El caso testigo, medido: `JUGUETE CARTAS INFANTILES SC`
tiene **71 unidades** en BIMS (57 en San Cosmos + 14 en el giftshop móvil) y la web dice **16**. De
20 juguetes y libros publicados, **casi todos figuran `outofstock` con stock 0** mientras el
inventario real vive en BIMS.

Hoy nadie sincroniza. Hay tres implementaciones y ninguna corre (ver
`project_sincronizacion_stock_bims` en la memoria): el plugin `bimsc` está **inactivo**,
`muci-bims-integrador` nunca se desplegó, y `syncstock` quedó en una rama 107 commits atrás. La
prueba dura de que nunca funcionó: la tabla `wpzv_bimsc_stocks` tiene **0 filas** desde que se creó
el 2025-10-13, y **ningún** producto de Woo tiene el meta `_bims_sync`.

## Objetivo

Que WooCommerce publique el stock que BIMS tiene, para los productos que BIMS declara
inventariables, sin apagar ventas por error.

## No-objetivos, explícitos

- **Precios.** `bimsc` los escribe (`sell_price × SellPriceCurrency.buy_price`, con un campo `PP`
  que manda si existe). Traer eso junto con el stock **cambiaría los precios del sitio**. Afuera.
- **Woo → BIMS.** Nada de esta spec escribe en BIMS. El descuento por venta ya funciona: la venta
  se crea y BIMS descuenta.
- **Entradas.** No tienen inventario en BIMS y quedan excluidas por el propio filtro `stockable`.
- **Crear productos.** Si BIMS tiene un producto que Woo no, se reporta; no se crea nada.

## Por qué es un sondeo y no un evento

**BIMS no tiene webhook de salida.** De los 340 paths de su openapi, los 11 que suenan a
webhook/notificación son entrantes o de otro circuito (callbacks de Bancard, notificaciones para
usuarios, alertas de WhatsApp). Y **ningún endpoint de stock acepta un filtro "modificado desde"**:
`availabilities`, `products_stocks` y `products` sólo tienen `limit`, `offset` y `plain`.

Así que "cuando ajustan en BIMS" sólo puede implementarse como **barrido periódico**. El pedido
original hablaba de un evento; esto es lo más cercano que la API permite, y conviene que quede
dicho para que nadie lo lea como una garantía de tiempo real.

## Lectura de BIMS

**Endpoint:** `GET /products/index.json?v_stock=1`, **paginado de a 100**.

- `v_stock=1` es lo que hace aparecer `AvailabilityFull`, que trae el stock por depósito. Sin ese
  parámetro no viene.
- **`limit=100`, no 500.** Medido: con 500 la respuesta **no entra en los 30 s de
  `TIMEOUT_LECTURA`** y da timeout. Y el reintento no salva nada, porque
  `PRESUPUESTO_REINTENTOS = 40` s se reparte entre los intentos: si el primero consume 30, al
  segundo le quedan ~8. La página tiene que ser chica para que reintentar tenga sentido. El código
  rescatado usa 500.
- Son 574 productos → **6 páginas** por barrido.

**Descartado: `availabilities/index.json`.** Pagina **sin orden estable**: devolvió 21 filas para 2
productos × 6 depósitos donde debían ser 12, con repetidas y distinto orden entre páginas. Un
barrido por ahí puede ver un producto dos veces y **perderse otro entero**.

**El número vendible es `total`, no `total2`.** `Product.availability`, que calcula BIMS, **es la
suma de `total`** — verificado en cuatro productos. `total2` trae negativos grandes y
proporcionales al volumen de venta (−285 en cartas infantiles, −63 en peluche): es una salida
acumulada, no stock disponible.

⚠️ `total` viene con formatos inconsistentes en la misma respuesta (`'0'`, `'0.000000'`,
`'128.0000000000000000'`). **Parsear como float**, nunca comparar como texto.

### Depósitos

Setting nuevo con la lista de ids habilitados. Arranca en **`6,7`** (San Cosmos y GIFTSHOP MÓVIL),
que es donde está **todo** el stock del catálogo:

| id | depósito | unidades hoy |
|---|---|---|
| 6 | San Cosmos | 1749 |
| 7 | GIFTSHOP MÓVIL | 1300 |
| 1 | Casa Matriz | **0** |
| 3, 4, 5 | Salón de Ventas, Alquilados, Tatakualab | 0 |

Va como setting y no en el código por una razón concreta: **Woo no sabe en qué depósito viven los
productos, sólo BIMS lo sabe**, así que la política de qué stock cuenta como vendible online no se
puede derivar de ningún lado y tiene que estar en un lugar explícito y auditable. Es lo que le faltó
al `bimsc_warehouses = 1,4` del plugin, que incluía **Alquilados** y nadie volvió a mirar en un año.

⚠️ **`/warehouses/index.json` no lista todos los depósitos:** devolvió 1, 4, 5, 6, 7 y se comió el
**3 (Salón de Ventas)**, que sí aparece en las disponibilidades. No usarlo como censo.

## Vínculo producto Woo ↔ producto BIMS

**El SKU de WooCommerce ES el id de producto de BIMS.** Confirmado sobre merch real: SKU 13 =
`JUGUETE MEDIDOR DE ALTURA (SPACE) - SC` id 13; SKU 16 = id 16; SKU 128 = `BOLSAS SC`. Es la misma
convención que ya usa `build_sale_products` para facturar.

**Por qué no las otras dos llaves:**

- **El `code` de BIMS no existe:** 0 de 427 productos `stockable` tienen `code` o `code2`. Nunca se
  puede matchear desde el lado de BIMS.
- **El meta `_bims_id` cubre menos y peor:** 186 productos publicados contra 350 del SKU, y tiene
  **7 valores repetidos** — 7 productos de BIMS apuntando a más de un producto de Woo. `bimsc`
  resuelve eso con `the_post()`, o sea toma el primero y descarta el resto **sin avisar**.

**Requisito de diseño:** la traducción SKU → id de BIMS tiene que ser **una sola función compartida**
con el camino de venta. Si cada camino la implementa, van a discrepar sobre qué producto de BIMS es
cada producto de Woo, y eso se descubre facturando mal. (15 de los 350 SKU no son numéricos:
`442-1`, `92-5`, `7-2`, con patrón `<id BIMS>-<sufijo>`.)

### La regla de herencia padre → variación

El nivel del vínculo **no es uniforme**: en los juguetes el SKU está en la variación, y en el libro
de colorear está en el padre. La regla, en orden:

| caso | qué se hace | volumen |
|---|---|---|
| la variación tiene SKU propio | se usa | 323 variaciones |
| no tiene, el padre sí, y es la **única** hermana sin SKU | **hereda del padre** | **hasta 32** (ver nota) |
| no tiene, y hay **varias** hermanas sin SKU | **se saltea y se reporta** | 59 variaciones en 12 padres |
| ni la variación ni el padre tienen SKU | sin vínculo, nada que hacer | 16 padres |

**Nota sobre el "hasta 32":** hay 32 padres con exactamente una variación sin SKU, y de los 44
padres con variaciones sin SKU, **28 tienen SKU** para heredar. La intersección exacta no se midió,
así que el caso de herencia cubre **entre 16 y 32** variaciones. Es un rango, no un dato: la
implementación lo va a contar de verdad en el barrido en seco.

La herencia es la semántica de WooCommerce (`WC_Product_Variation::get_sku()` hereda del padre), así
que no es un parche. **Pero se rompe con varias hermanas:** `Taza pequeña` tiene SKU 27 en el padre
y **9 variaciones sin SKU** —nueve diseños de la misma taza—. En BIMS el producto 27 tiene *un*
stock; heredar ahí escribiría el mismo número nueve veces, o sea **inventario multiplicado por 9**.
Es detectable, así que el barrido no tiene por qué adivinar: se saltea y se informa.

**Consecuencia aceptada (Carlos, 2026-09-02):** esas 59 variaciones siguen con el stock que Woo
tenga hoy, igual que ahora — no hay regresión. Y la contabilidad no se rompe porque **la venta de
esas variaciones se resta del padre en BIMS**. Lo que queda pendiente es corregirles el SKU en Woo;
el barrido produce esa lista. Los tres casos grandes son `Libros Ttklab` (23), `Taza pequeña` (9) y
`Pines` (4).

Salvedad para esa conversación: puede que esos nueve diseños **legítimamente compartan una sola
línea de stock en BIMS**. Si es así, el problema no es el SKU faltante sino que BIMS no los modela
por separado, y Woo no debería fingir que sí. La sincronización no puede resolver eso; sólo mostrarlo.

## Escritura en WooCommerce

Sólo tres campos: **`stock_quantity`**, **`manage_stock: true`** y **`stock_status`**
(`instock` / `outofstock`).

**Sólo se escribe donde el número difiere.** El barrido arranca enumerando lo publicado en Woo, y en
esa misma respuesta **ya viene el stock actual**, así que la comparación sale gratis: no hace falta
tabla nueva ni migración para saber qué cambió, y no se escribe nada cuando no cambió nada.

**Costo por barrido:** ~1 request para los productos simples + **36** para las variaciones de los
padres que las tienen (las 323 variaciones con SKU pertenecen a sólo 36 padres) + **6** a BIMS.
Cada 15 minutos, despreciable.

Esto también responde por qué **no** se hace como plugin de WordPress, que es donde `bimsc` tiene
su única ventaja real: ahí enumerar variaciones es gratis (`wc_get_products`, `get_children`). Pero
viviría fuera del repo y de las 241 pruebas, se desplegaría por otra vía, y sumaría PHP al stack.
Pagar mantenimiento de un plugin para ahorrar 36 requests cada 15 minutos no cierra.

## Guardas

Tres, y ninguna es opcional:

1. 🔴 **Nunca escribir por una lectura fallida.** Si una página de BIMS falla, los productos de esa
   página **no se tocan**. Sin esto, una caída de BIMS apaga el catálogo público. La regla sale de
   un error propio: un sondeo de diagnóstico imprimió "NO EXISTE en BIMS" nueve veces cuando en
   realidad BIMS había dado timeout. Distinguir "no hay dato" de "el dato es cero" es la diferencia
   entre una web correcta y una tienda cerrada.
2. **Guarda de radio de impacto.** Si un barrido pondría en cero a más de `N` productos de golpe, se
   **detiene, no escribe nada y avisa a Slack**. Un cero masivo casi siempre es un problema de la
   consulta o del depósito configurado, no que se haya vendido todo. `N` va como setting, con
   valor inicial **5** (el razonamiento y la medición que lo sostienen, más abajo).
3. **El primer barrido corre en seco.** Calcula todo, informa qué cambiaría y **no escribe**. Con
   esa lista a la vista se confirma que los depósitos elegidos son los correctos, antes de que la
   web se entere. El modo seco queda disponible como flag para siempre.

Nota de expectativa: **el primer barrido real va a encender productos, no apagarlos** — hoy la web
tiene decenas de productos en `outofstock` que en BIMS tienen stock. La guarda 2 mira ceros; una
subida masiva es el resultado esperado y correcto.

## Cadencia

Comando de management propio, cron **cada 15 minutos**, envuelto en `flock` como
`process-queue.sh`. El peor desfase es un cuarto de hora, suficiente para merch donde no hay pelea
por la última unidad.

Y con el `cd /var/www/integrador` en el script: `settings.py` lee el `.env` con `dotenv_values()`,
que es **ruta relativa**, y desde el home del cron revienta antes de llegar a Django.

## Auditoría

Cada barrido deja registro de qué escribió, con el **desglose por depósito** del número publicado.
No es prolijidad: como Woo no sabe en qué depósito vive nada, cuando alguien pregunte "por qué dice
3" la respuesta no está en la web ni en Woo. Sin el desglose, ese número no se puede explicar
después.

Y el log del cron necesita `logrotate` desde el día uno. `/var/log/process-queue.log` se instaló sin
rotación el 2026-09-02 y ya es deuda.

## Errores

- **Página de BIMS que falla** → esos productos no se tocan, se cuenta y se sigue con las demás.
- **Producto de BIMS sin correspondencia en Woo** → se reporta, no se crea.
- **Variación ambigua** (varias hermanas sin SKU) → se saltea, se reporta.
- **Escritura a Woo que falla** → se cuenta, se sigue con el resto del lote, se reintenta en el
  barrido siguiente. La escritura es idempotente.
- **Todo el barrido falla** → avisa a Slack con la clave propia, reusando `core/alerts.py`, que ya
  tiene throttle en base y es fail-safe.

## Testing

- La lógica de suma por depósito, la regla de herencia de SKU y el cálculo de diferencias son
  funciones puras: se prueban sin red.
- Las tres guardas necesitan test propio, sobre todo la 1 (lectura fallida no escribe) y la 2
  (radio de impacto), que son las que evitan apagar la tienda.
- Los mocks de la API de Woo **tienen que devolver la forma real**, con `meta_data` y `sku`
  incluidos. Ya nos pasó que un mock irreal —`{"id": ...}` sin `meta_data`— dejaba un agujero con la
  suite en verde.
- Nada de tests que salgan a la red. La verificación del Despliegue 2 delató un `GET` real a
  WooCommerce que el `except` del lote se tragaba; el parche fue en `setUp`.

## Decisiones cerradas el 2026-09-02

**`N = 5` para la guarda de radio de impacto.** Elegido con datos, no por analogía con otro setting:

- **La población en riesgo son 44 productos** publicados con stock > 0 en Woo (10 simples + 34
  variaciones), de los cuales **41 tienen SKU propio** y por lo tanto son los que el barrido puede
  tocar. Ese es el techo de un desastre.
- **El ritmo real de venta** en los últimos 14 días fue de 3 a 20 productos distintos por día. Pero
  eso es *vendidos*, no *llegados a cero*: hay 2657 unidades en 44 productos, ~60 promedio cada uno.
  En una ventana de 15 minutos lo esperable es que **0 o 1** producto agote su última unidad.
- **5 está arriba del ruido y abajo del desastre:** es el 12% de los 41, así que un error sistémico
  lo dispara en el primer barrido, y a la vez cinco productos agotándose en la misma ventana de 15
  minutos sería extraordinario.
- **La asimetría del costo favorece el número bajo:** si salta de más, cuesta un mensaje y 15
  minutos, y el barrido siguiente reintenta. Si no salta cuando debía, cuesta la tienda cerrada sin
  que nadie se entere.

⚠️ **Revisar si la población con stock pasa de ~100 productos**; con el catálogo de hoy, 5 es
holgado, con el triple sería apretado.

**Cuando la guarda salta se aborta el barrido COMPLETO**, no sólo las bajadas a cero: si el dato
está sistémicamente mal —depósito equivocado, respuesta rara de BIMS— entonces las **subidas también
están mal**, y aplicar la mitad de un dato contaminado es peor que no aplicar nada.

**Salón de Ventas (depósito 3): fuera de alcance.** Decidido por Carlos. Hoy tiene 0 unidades y no
entra en el setting. Si alguna vez importa, se agrega el id y se reinicia.

**Los 12 padres con variaciones ambiguas: no se tocan.** Decidido por Carlos. Esas 59 variaciones
siguen con el stock que Woo tenga, igual que hoy, y la contabilidad no se rompe porque la venta se
resta del padre en BIMS. El barrido igual las reporta en cada pasada, así que la lista está
disponible el día que se quiera actuar — pero **no es un pendiente de esta spec**.

## Preguntas abiertas

Ninguna. Las tres que había quedaron cerradas arriba.

## El veredicto de la comparación

Carlos pidió comparar el código rescatado contra lo aprendido de `bimsc` **sin dar por sentado que
`bimsc` gana**. Quedó repartido, y en la decisión más importante no gana ninguno:

| decisión | gana | por qué |
|---|---|---|
| Sumar `total` | **empate**, los dos bien | y una alarma mía de que `total` era siempre 0 quedó **refutada** |
| Filtrar depósitos | `bimsc` | el rescatado suma todo — aunque hoy da el mismo número |
| Estrategia de consulta | `bimsc` | pregunta sólo por lo vinculado; el rescatado crawlea con `limit=500`, que **da timeout** |
| **Llave de vínculo** | **ninguno** | `_bims_id` cubre 186 con 7 ambiguos; el filtro por meta del rescatado **no existe en la REST de Woo**. Gana el **SKU**, que ya usa el integrador |
| Arquitectura | **ninguno** | el rescatado le pega por HTTP a su propio Django; `bimsc` necesita vivir dentro de WordPress |

De `core/stock_sync_rescatado.py` se reutilizan ~40 líneas: cómo suma las disponibilidades y la
forma del payload de escritura. El resto se reescribe.

⚠️ **No mergear `feature-sync` para traer el original.** Ese commit (`c6a87dd`) mete además
`SimpleForgotPasswordView` / `ForgotPasswordView`, que **generan una password temporal y la
devuelven en el cuerpo de la respuesta HTTP** además de loguearla. `main` hoy no lo tiene.

## Aparte, no es de esta spec

Las opciones del plugin `bimsc` guardan **usuario y contraseña de BIMS en texto plano** en
`wpzv_options`, y el plugin está inactivo desde 2025. Conviene borrar esas opciones y **rotar esa
credencial**.
