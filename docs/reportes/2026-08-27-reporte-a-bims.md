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

- **Endpoint:** `POST /api/sales/` (respuesta, no petición).
- **Ubicación en el JSON:** `data.Agency.tae_password`.
- **Lo detectamos en:** venta `Sale.id = 31301`, del 2026-08-27 a las 19:41 UTC.
- **Antigüedad real del comportamiento:** al revisar nuestros registros históricos, el
  campo aparece en respuestas de **marzo de 2026** y en **todas** las ventas exitosas
  desde entonces. El 2026-08-27 es cuando lo notamos, no cuando empezó.

**No transcribimos el valor en este documento.** Pero hay un punto incómodo que
preferimos decir de frente, porque cambia el tamaño del problema: **no podemos
afirmar que esa credencial siga contenida dentro de nuestra infraestructura.** El
detalle está en la sección siguiente.

## Por qué nos parece importante

Registrar el cuerpo crudo de las respuestas es práctica estándar en una integración
entre dos sistemas que cambian con frecuencia: es lo que permite diagnosticar un
rechazo de la SET o un cambio de contrato sin reproducir la venta. Nuestro integrador
lo hace, y suponemos que cualquier otro cliente de la API también.

La consecuencia es que **esa contraseña se persiste en los logs de todos los
integradores, en cada venta exitosa**, sin que ninguno lo haya pedido ni lo sepa.

## Alcance de la exposición de nuestro lado

Auditamos dónde terminó el dato en nuestro caso. Lo detallamos porque creemos que
cualquier integrador con una arquitectura parecida está en la misma situación, y eso
es información que necesitan para evaluar el impacto:

1. **Archivos de log del servidor**, replicados por la rotación en varios archivos.
2. **Copias de esos logs en equipos de desarrollo**, usadas para depuración. Solo en
   uno de esos conjuntos contamos **307 apariciones** del campo.
3. **Un servicio externo de seguimiento de errores.** Nuestra configuración envía los
   registros de nivel INFO como contexto adjunto a los eventos de error. Es decir: es
   muy probable que la credencial **ya haya sido transmitida a un proveedor tercero**,
   fuera de nuestro control y del de ustedes. Esto es responsabilidad de nuestra
   configuración, no de BIMS, y lo estamos corrigiendo — pero **es irreversible para
   los datos ya enviados**, y por eso el pedido 1 de abajo es urgente.

Un integrador que use cualquier plataforma de observabilidad (Sentry, Datadog,
CloudWatch, ELK) tiene el mismo problema sin haberlo elegido, porque el dato llega
dentro de una respuesta que es legítimo registrar.

## Qué pedimos

1. **Rotar esa credencial (`tae_username` / `tae_password`), asumiendo que estuvo
   expuesta.** Lo ponemos primero porque es lo único que remedia el pasado: quitar el
   campo de la respuesta protege de acá en adelante, pero no deshace cinco meses de
   logs ni lo que ya se transmitió a terceros. Conviene asumir la exposición en los
   registros de **todos** los clientes de la API durante todo ese período.
2. **Quitar `tae_password` de la respuesta de `/api/sales/`.** Si el campo se necesita
   internamente, que no se serialice hacia la API. Lo mismo aplica a cualquier otro
   endpoint que devuelva el objeto `Agency`.
3. **Confirmarnos si algún otro endpoint devuelve campos de este tipo**, para que
   podamos revisar nuestros registros históricos con criterio en vez de a ciegas. Si
   nos dan la lista, la usamos para purgar con precisión.
4. **Avisar a los demás integradores**, si corresponde. Nosotros lo encontramos por
   casualidad revisando otra cosa; no tenemos motivo para pensar que somos los únicos
   afectados ni los primeros en tenerlo en disco.

## Qué vamos a hacer nosotros

- **Redactar los campos sensibles antes de escribir el log**, filtrando de forma
  recursiva las claves que coincidan con `password`, `secret`, `token`, `api_key` y
  similares. Nuestro enmascarado actual no lo atrapó porque comparaba nombres exactos
  y solo en el primer nivel del JSON.
- **Cortar el envío de cuerpos de respuesta al servicio externo** de seguimiento de
  errores.
- **Purgar las copias existentes** de los logs, en el servidor y en los equipos de
  desarrollo.

Nada de esto reemplaza al arreglo del lado de BIMS: mitiga nuestro caso, no el de los
demás integradores, y no alcanza a lo ya transmitido.

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
