# Handoff sesión del 2026-08-26

> Sesión de dos actos: se validó el despliegue de timeouts del día anterior y se implementó
> entera la feature del presupuesto por orden, que es lo que cierra el hallazgo 1. **Nada se
> desplegó.** Producción sigue en `main` (`3b9773c`) todo el tiempo.

## Estado al cierre

| | |
|---|---|
| **Producción** | rama `main`, commit `3b9773c` — **sin cambios en toda la sesión** |
| Rama de trabajo | `feature/presupuesto-por-orden` @ `13af3b8`, pusheada |
| Tests | **152/152** en local (3.12 + Django 6) **y** en el stack de producción (3.7.17 + Django 3.2.18) |
| Servicio | último arranque 12:00 UTC — el reinicio programado, anterior a esta sesión |
| Falta para desplegar | smoke test contra BIMS real; ver "Lo que sigue" |

---

## 1. El sync nocturno salió limpio y cerró la validación de los timeouts

Primera corrida del código de timeouts desplegado el 25/08, con 38 llamadas a BIMS:

- `Total guardados: 17.304`
- **0** `Presupuesto de 40s agotado`, **0** `ReadTimeout`, **0** `ConnectTimeout`
- Ninguna orden pausada, ningún error nuevo

Y hubo orden facturable real: **201219 → BIMS Sale 31271**, en ~10 s. Van 5 órdenes facturadas
desde el deploy (31267 a 31271) sin un solo fallo. **El presupuesto de 40 s no hay que
ajustarlo.**

**Confirmado de paso un pendiente:** `bims_api.log.1`, `.2` y `.3` están los tres fechados
00:03. El cron nocturno quema las 4 ventanas de rotación en tres minutos, **todas las noches**.
Ya no es hipótesis.

---

## 2. El presupuesto por orden, implementado

Spec: `docs/superpowers/specs/2026-08-25-presupuesto-por-orden-design.md`
Plan: `docs/superpowers/plans/2026-08-26-presupuesto-por-orden.md`

```
13af3b8 docs: encabezados Task N en el plan
4de77a4 fix: 4 hallazgos del review final de la rama
b2baa85 feat(services): presupuesto por orden y FailedOrder al leer productos
aa263fd docs(woocommerce): documentar el supuesto de concurrencia
c22b9c4 feat(woocommerce): recortar el timeout al restante de la orden
247caf7 feat(bims): respetar el presupuesto de la orden y recortar la conexión
69cadfd feat(deadline): modulo del presupuesto por orden con ContextVar
```

`core/deadline.py` lleva el límite en un `contextvars.ContextVar`. `PRESUPUESTO_ORDEN = 90`.
Lo fija **solo** `process_order`, con `try/finally`. `bims.py` y `woocommerce.py` recortan sus
timeouts al mínimo entre el propio y el restante de la orden. Quien no fija presupuesto —el
cron `sync_bims_contacts`— recibe `None` de `restante()` y **no requirió tocar ese archivo**.

### Una brecha que la spec daba por inexistente

La spec afirmaba que `process_order` ya grababa `FailedOrder` ante cualquier excepción. **Es
falso.** Tiene cuatro `try/except` separados y la llamada a `build_sale_products` no estaba en
ninguno — justo donde vive el `get_product` por ítem, el tramo que escala con el tamaño de la
orden. Una excepción ahí se escapaba **sin registro**.

Ya estaba roto antes de esta rama: una `ServerException` de WooCommerce en ese bucle se perdía
igual. Cerrado en `b2baa85`.

### Dos defectos que el plan introdujo, encontrados por la revisión final

Ninguno lo habría visto una revisión por tarea. Los dos ya están arreglados en `4de77a4`.

1. **`_alternate_base_url` no es una consulta pura.** Muta `self.base_url` de forma pegajosa y
   *después* devuelve la URL. La guardia nueva lo llamaba primero y abandonaba después, así que
   con el presupuesto agotado la instancia quedaba apuntando a `BIMS_FALLBACK_URL` **sin mandar
   un request y sin loguearlo** — el `warning` estaba debajo de la guardia. Producción tiene la
   fallback configurada: era routing real.
2. **Un timeout escalar en `requests` aplica a conexión Y lectura por separado.** El
   `_timeout_efectivo` de WooCommerce devolvía un escalar, así que una llamada recortada a N
   podía gastar 2N. Peor caso ~115 s contra los 120 de gunicorn: **el margen de 30 s que
   promete la spec no existía.** Es el mismo defecto que el hallazgo 2 obligó a arreglar del
   lado de BIMS, repetido del lado de WooCommerce. Arreglado con tupla `(connect, read)`.

---

## 3. Verificado sobre el stack real

152/152 con **Python 3.7.17 + Django 3.2.18**, y los 5 archivos tocados compilan en 3.7.

**Cómo, porque el método del plan no se pudo usar.** `anthropic_readonly` no puede leer `/root`
(donde vive el venv), ni escribir en `/var/www/integrador/.git` (así que no hay `worktree add`),
ni ejecutar el intérprete del venv. Lo que sí funciona:

```
git archive --format=tar HEAD | ssh ... 'mkdir -p ~/wt && tar -x -C ~/wt'
cd ~/wt && /usr/bin/python3.7 manage.py test core/ --settings=muci-integrador.test_settings
```

El `python3.7` del sistema tiene Django 3.2.18, `requests` 2.25.1, `woocommerce` 3.0.0,
`sentry_sdk`, `dotenv` y DRF 3.15.1.

⚠️ **Salvedad:** eso es Django **3.2.18**, no el **3.2.25** exacto del venv de producción — 7
releases de parche dentro de la misma minor. Prueba compatibilidad con 3.7 y con la API de
Django 3.2; no prueba el venv exacto. **La corrida contra el venv real necesita root.**

El único warning en la salida es un `CryptographyDeprecationWarning` de `pymysql`, preexistente
y ajeno a esta rama.

---

## Lo que sigue, en orden

1. **Smoke test de solo lectura contra el BIMS real**, desde el servidor, con el deadline
   fijado a mano: verificar que la tupla `(connect, read)` viaja bien y que `get_contacts`
   responde. **Bloqueado para `anthropic_readonly`**: necesita leer el `.env`. Lo tiene que
   correr alguien con root.
2. **Opcional pero barato:** correr la suite con el venv real (`/root/.local/share/virtualenvs/integrador-ObaHlHmv/bin/python`)
   para cerrar la salvedad de 3.2.18 vs 3.2.25. También necesita root.
3. **Desplegar**, y mirar la corrida del cron de las 00:00 UTC como canario — 38 llamadas
   secuenciales es el mejor banco de pruebas del proyecto para cualquier cosa del transporte HTTP.
4. **Recién con eso estable unos días, discutir sacar el reinicio cada 6 horas.**

## Lo que NO cubre esta rama

- **Nada acota una respuesta que gotea bytes.** El read timeout de `requests` es por lectura de
  socket, no por respuesta. Un BIMS que manda un byte cada 20 s pasa todos los recortes y
  revienta los 120 s igual. El presupuesto defiende contra el silencio, no contra la lentitud
  con pulso. **Si siguen apareciendo signal-kills después del deploy, mirar acá primero.**
- **`get_razon_social` es una tercera llamada externa dentro de la orden que nadie recorta.**
  `ruc.py:39` usa `timeout=5` escalar (hasta ~10 s), una vez por orden con RUC. Es fail-safe,
  entra en el margen, pero la aritmética de la spec no la contempla.

### Se decidió NO subir el `--timeout` de gunicorn

Se evaluó pasarlo a 180 s. **No compra nada:** el presupuesto de 90 s corta primero, así que
ninguna orden real llega a 120 hoy (peor caso del diseño ≈ 105 s). Solo ensancha la red para el
caso del goteo, que es ilimitado igual. Y cuesta: un worker realmente colgado queda atado 180 s
en vez de 120, y con `--workers 3` son tres minutos sin atender en vez de dos. Si alguna vez
hace falta más margen, la palanca correcta es **bajar `PRESUPUESTO_ORDEN`**, no subir gunicorn.

## Pendientes que vienen de antes (sin cambios)

- **⚠️ Backups de las bases.** Sigue sin resolverse. Más grave que cualquier parche.
- **201 órdenes en FAILED** sin reproceso automático; `runretryfaileds.sh` es código muerto y roto.
- **Los logs de BIMS se pierden ~12 h por día** (confirmado otra vez esta sesión).
- **Spec del proyecto B** (Python 3.12 + Django 5.2 LTS).
- **Ventana para los 67 parches de terceros** y decidir sobre Ubuntu Pro.
- Renombrar la fila del posale 7 a "Caja Fund MuCi" desde el admin.
- Nombres deformados tipo `C L A R I C E` en el reproceso, sin diagnosticar.
- Borrar `feature/gestion-sucursales`, `feature/migracion-api-key` y `feature/timeouts-bims`
  (ya contenidas en `main`).

## Menores anotados y no arreglados

Del review final, todos evaluados y parkeados a propósito: el mensaje de agotamiento reporta
`PRESUPUESTO_ORDEN` en vez del presupuesto en efecto si alguien pasa uno custom; un intento
fútil de ~2 s cuando queda una fracción de segundo; dos handlers de `FailedOrder` sin `status`
explícito (`services.py:504,540`); `resolve_pos_and_payments` sin envolver (no puede lanzar
excepciones de presupuesto, pero sí `ValueError`/`KeyError`); y dos cosas dormidas que solo
aplican en modo sesión —el relogin duplica el gasto de un intento, y `str(e)` puede arrastrar
un `?sid=` a la base—. Producción corre en modo API Key desde el 24/08.
