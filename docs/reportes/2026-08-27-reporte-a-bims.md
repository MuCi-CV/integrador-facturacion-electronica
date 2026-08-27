# Reporte a BIMS — 2026-08-27

> **Cómo usar este documento:** son **dos mensajes independientes**. La Parte 1 es un
> problema de seguridad y conviene enviarla **sola**, para que no pierda urgencia
> mezclada con consultas técnicas. La Parte 2 puede ir después, por el canal habitual.
>
> Contexto del emisor: Museo de Ciencias (MuCi). Integración WooCommerce → BIMS para
> facturación electrónica continua. Tenant en `in.bims.app`.

---

# PARTE 1 — Seguridad: la respuesta de `/sales/` expone una contraseña en texto plano

**Prioridad: alta. No requiere que hagamos nada de nuestro lado para reproducirlo.**

## Qué encontramos

Al revisar el cuerpo de la respuesta de un `POST /api/sales/` detectamos que BIMS
devuelve, dentro del objeto **`Agency`**, un campo **`tae_password` con una contraseña
en texto plano**, acompañado de **`tae_username`**. El par de credenciales viaja
completo en la respuesta de cada venta creada.

- **Observado en:** venta `Sale.id = 31301`, creada el 2026-08-27 a las 19:41 UTC.
- **Endpoint:** `POST /api/sales/` (respuesta, no petición).
- **Ubicación en el JSON:** `data.Agency.tae_password`.

**No transcribimos el valor en este documento** ni lo enviaremos por ningún canal.

## Por qué nos parece importante

Registrar el cuerpo crudo de las respuestas es práctica estándar en una integración
entre dos sistemas que cambian con frecuencia: es lo que permite diagnosticar un
rechazo de la SET o un cambio de contrato sin reproducir la venta. Nuestro integrador
lo hace, y suponemos que cualquier otro cliente de la API también.

La consecuencia es que **esa contraseña se persiste en los logs de todos los
integradores, en cada venta exitosa**, sin que ninguno lo haya pedido ni lo sepa. En
nuestro caso quedó replicada en varios archivos por la rotación del log.

## Qué pedimos

1. **Quitar `tae_password` de la respuesta de `/api/sales/`.** Si el campo se necesita
   internamente, que no se serialice hacia la API. Lo mismo aplica a cualquier otro
   endpoint que devuelva el objeto `Agency`.
2. **Rotar esa credencial**, dando por supuesto que estuvo expuesta en los logs de
   todos los clientes de la API durante el tiempo que lleve el comportamiento.
3. Confirmarnos si **algún otro endpoint** devuelve campos de este tipo, para que
   podamos revisar nuestros registros históricos con criterio en vez de a ciegas.

## Qué vamos a hacer nosotros

Independientemente de la respuesta, vamos a **redactar los campos sensibles antes de
escribir el log** (filtrando claves que coincidan con `password`, `secret`, `token`,
`api_key`). No reemplaza al arreglo del lado de BIMS: mitiga nuestro caso, no el de los
demás integradores.

---

# PARTE 2 — Consultas técnicas sobre la API

Tres temas, en orden de impacto para nosotros.

## 2.1 Órdenes de uso interno: falta el endpoint de creación

`GET /api/stock_uses/index.json` existe y lista las órdenes de uso interno. **No existe
un `add`.**

Nuestro caso de uso: registrar consumos que **mueven inventario sin emitir factura**
(cortesías, consumo interno, obsequios). Por lo que entendemos de la ayuda, las Órdenes
de Uso Interno son exactamente el mecanismo previsto para eso.

**Consulta:** ¿está previsto exponer `stock_uses/add` por API? Si no, ¿cuál es el
mecanismo recomendado para ese caso desde una integración?

Como alternativa evaluamos `POST /api/invads/add.json` (ajuste de inventario), pero no
nos parece adecuado para automatizar por pedido: `reported_quantity` es un **conteo
absoluto**, lo que obliga a leer-modificar-escribir con riesgo de carrera si hay ventas
concurrentes; y según la ayuda los ajustes **generan asientos contables de ganancia o
pérdida**, que no es lo que corresponde a una cortesía. Si estamos interpretando mal
alguna de las dos cosas, agradecemos la corrección.

## 2.2 ¿Una venta con `billed: false` descuenta inventario?

`billed` aparece como campo escribible en `SaleJsonRequest.Sale`. Verificamos que una
venta facturada normal **sí** descuenta stock (`Sale.stock: true`,
`SalesProduct.stock: true`).

**Consulta:** ¿una venta creada con `billed: false` descuenta inventario igual? ¿Y qué
ocurre con la certificación electrónica en ese caso — se omite, o se envía igual?

No lo probamos porque una prueba equivocada **emite una factura electrónica real ante
la SET**, y preferimos preguntar antes que generar un comprobante que después haya que
anular.

Relacionado: `document_type` **no** figura entre los campos escribibles de
`SaleJsonRequest.Sale`. ¿Es correcto que no se puede fijar por API?

## 2.3 Documentación: tres desajustes

1. **`ayuda.bims.app/api` documenta 3 endpoints** (productos, monedas, suscripción de
   tenants), mientras que el `openapi.json` que nos compartieron declara **340 paths**.
   Endpoints que usamos a diario —`/sales/`, `/contacts/`, `/posales/`— no están en la
   ayuda publicada. Nos costó tiempo real: concluimos que no existía API de stock
   cuando sí existe.

2. **La página `/stock/ordenes-de-uso-interno` está vacía** — tiene título y ningún
   contenido.

3. **Autenticación por API Key.** El `openapi.json` declara el formato
   `Authorization: Bearer {tenant}_{api_key}`, mientras la ayuda publicada muestra la
   key **cruda** (incluido `X-API-Key: <key>`). En nuestras pruebas **el formato con
   prefijo de tenant devuelve 401** y los de la ayuda publicada funcionan. Convendría
   corregir el `openapi.json`. Nos importa en concreto porque tenemos pendiente la
   migración a API Key y entendemos que el soporte de sesión (`?sid=`) tiene fecha de
   corte.

**Consulta final:** ¿cuál de las dos fuentes debemos tomar como contrato — la ayuda
publicada o el `openapi.json`? Hoy asumimos que gana la ayuda publicada cuando se
contradicen, pero preferimos confirmarlo.

---

*Preparado por el equipo del integrador de MuCi. Los detalles técnicos (payloads,
ids de venta, timestamps) están disponibles si les sirven para reproducir.*
