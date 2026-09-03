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

### El destino de escritura: un producto de BIMS, un destino

> ⚠️ **Revisado el 2026-09-03.** La versión anterior de esta sección definía una regla de
> herencia **por variación** —"si es la única hermana sin SKU, hereda el del padre"— y el barrido en
> seco demostró que **fabrica vínculos**. Se reemplaza por la regla de abajo. El detalle de lo que
> salió mal está en "Lo que encontró el barrido en seco", al final.

El catálogo se crea con **un producto de BIMS por producto simple o por variación** (Carlos,
2026-09-03). De ahí la regla, que tiene un solo criterio:

| caso | destino de escritura | volumen medido 2026-09-03 |
|---|---|---|
| producto simple con SKU propio | **él mismo** | 90 |
| variación con SKU **propio** | **la variación** | 318 |
| variación **sin** SKU propio (hereda) | **el padre, UNA sola vez** | 26 padres, 76 variaciones |
| ni la variación ni el padre tienen SKU | sin vínculo, nada que hacer | — |

**434 destinos en total.** El punto de la regla es que **el número de destinos no depende de cuántas
variaciones hereden**: si nueve diseños de `Taza pequeña` heredan el SKU 27, el destino sigue siendo
uno —el padre— y las 16 unidades de BIMS se publican una vez.

#### Cómo se distingue "SKU propio" de "SKU heredado"

⚠️ **La REST de WooCommerce no lo dice.** `WC_Product_Variation::get_sku()` devuelve el del padre
cuando la variación no tiene propio, así que "tiene el 575" y "hereda el 575" llegan **idénticos** en
el JSON. No hay campo que los separe y `postmeta` no se expone.

Se resuelve **comparando con el SKU del padre**, y eso es válido por un invariante del catálogo
**medido el 2026-09-03**: no existe ningún SKU propio numérico repetido entre productos y
variaciones (consulta sobre `wpzv_postmeta`, **0 filas**). Entonces `sku == sku_del_padre` implica
heredado.

El invariante es la base de la regla, así que si algún día se rompe hay que enterarse: para eso está
la **guarda de colisión** (`core.stock.colisiones`), que aborta el barrido si dos destinos reclaman
el mismo producto de BIMS. Con este modelo no debería dispararse nunca.

#### Las variaciones que heredan pero llevan su propio contador

De las 76 que heredan, **22 tienen `manage_stock=yes` propio** (8 padres, todo merch: tazas 27 y 28,
`Tazas Pequeñas SC` 162, bolsas 8, remeras 24, posters 29, stickers 108). WooCommerce usa el
contador de la variación, así que **el número escrito en el padre no gobierna su venta**.

**Y eso está bien, no es un defecto (Carlos, 2026-09-03): esos contadores los mantiene la
cajería.** BIMS tiene un producto por **tipo** de taza y no por diseño —`bims 27` es "TAZA PEQUEÑA -
TTKLAB", y no conoce "Newton", "Pato" ni "Muci rosa"—, así que el detalle por diseño **sólo existe
del lado de Woo** y nadie más lo puede llevar. De ahí la regla: **se escribe el stock del padre y no
se toca el de los hijos**, y aplica sólo donde el padre tiene SKU y los hijos no.

El barrido las lista igual, por una razón distinta de la que decía la versión anterior de esta
sección: no es una lista de pendientes, es para que el stock del padre no se lea como si fuera lo
vendible de esas variaciones. Casi todas están hoy en 0; la excepción es `14124` (`Tazas Pequeñas
SC`) con 88 unidades y venta el 30/08.

⚠️ **Consecuencia asumida:** para esos productos, publicar el stock del padre **no cambia lo que la
web vende**. El número queda como referencia de BIMS, y lo vendible sigue siendo lo que la cajería
mantiene por diseño.

⚠️ **Riesgo latente, no vivo hoy:** si un padre-destino recibiera 0 de BIMS mientras sus hijos
autogestionados tienen unidades, `update_product_stock` le escribiría `stock_status: outofstock`, y
no está verificado si WooCommerce respeta ese estado en un producto variable —podría ocultar el
producto entero. **Medido el 2026-09-03: no puede pasar ahora**, porque ninguno de los 5 destinos
que van a 0 es un padre variable (4 son variaciones con SKU propio y 1 un producto simple, ninguno
con variaciones colgando).

### No se filtra por `status="publish"`

**295 de las 318 variaciones con SKU propio cuelgan de padres `private`** (medido 2026-09-03), y no
es un descuido del catálogo: así vive el **POS de FooEvents** (`fooeventspos_variation_show_in_pos =
yes`). Un producto `private` **se vende** en boletería.

Pedir sólo `publish` las dejaba afuera a todas, y **ésa era la causa real** de los "422 productos
inventariables de BIMS sin contraparte en WooCommerce" que reportó el primer barrido en seco. El
filtro por estado lo hace `core.stock.ESTADOS_VENDIBLES` = `("publish", "private")`, que sí deja
afuera `draft` y `trash`.

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
- **Variación sin SKU propio** → no es destino; el destino es el padre, una vez.
- **Variación que hereda y gestiona su propio stock** → se reporta, no se toca.
- **Dos destinos al mismo producto de BIMS** → aborta el barrido y avisa (no debería pasar).
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

**Las variaciones que llevan su propio contador: no se tocan, y no hay nada que arreglar.**
Decidido por Carlos el 2026-09-03: los mantiene la cajería, porque BIMS no modela el diseño. La
contabilidad no se rompe, porque la venta se resta del producto del padre en BIMS. **No es un
pendiente**: es cómo se opera.

**Publicar stock implica gestionarlo.** `update_product_stock` escribe `manage_stock: True`, así que
un destino que hoy vende ilimitado queda gobernado por el número de BIMS. Son **14 de los 26
padres**. Es inherente —no se puede publicar un stock sin que limite— y Carlos lo asumió
explícitamente el 2026-09-03. El barrido **lista esos destinos aparte en cada corrida**, para que el
cambio se vea en el seco antes de aplicarse.

## Lo que encontró el barrido en seco (2026-09-03)

La primera corrida en seco sobre producción invalidó tres cosas que esta spec daba por establecidas.
Queda anotado porque el modo seco existía exactamente para esto:

1. **La herencia por variación fabricaba vínculos.** `188079` y `188080` (`Ciencias de la tierra`,
   Online y En puerta) no tienen SKU propio y cada una recibía las 16 unidades del `bims 575`: **32
   publicadas donde hay 16**. La guarda de ambigüedad no lo frenó porque **nunca puede dispararse**:
   contaba "hermanas sin SKU" leyendo el SKU de la REST, que ya viene heredado, así que `sin_sku`
   daba 0. Era código muerto.
2. **Los "422 inventariables sin contraparte" eran un artefacto** del filtro `status="publish"`, no
   un hueco del catálogo.
3. **La coincidencia de depósitos no estaba confirmada.** La lectura de "44 de 48 ya coinciden" era
   un mal razonamiento: `calcular_cambios` saltea sin distinguirlo todo producto que BIMS no
   devolvió, así que "no hay dato" y "coincide exacto" se ven iguales en esa cuenta. Los depósitos
   `6,7` siguen siendo **plausibles** por la medición del 2026-09-02 (Casa Matriz en 0 sobre los 427
   inventariables), pero **el seco no los confirmó**: hay que volver a mirarlo con el modelo nuevo.

## Preguntas abiertas

**Los 176 productos de Woo sin SKU propio ni en la variación ni en el padre.** Se venden —`124861`
lleva 2847 líneas, `192638` vendió el 01/09— y se facturan contra el producto de BIMS del padre por
la herencia de la REST. Para el **stock** ya está resuelto (el padre es el destino). Lo que queda
por decidir, y no es de esta spec, es si esa imputación contable es la deseada o si a esas
variaciones les falta SKU propio.

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
