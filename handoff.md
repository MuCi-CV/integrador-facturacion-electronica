# Handoff — sesión del 2026-08-24

> Sesión de diagnóstico y arreglo. Se buscó la causa del corte de facturación del viernes, se
> descartó una primera hipótesis con evidencia, se encontró la verdadera —una cookie— y se arregló,
> validó contra la API viva y desplegó el mismo día. La migración a API Key vuelve a estar en
> producción, esta vez con los tres agujeros conocidos cerrados.

## Estado al cierre

| | |
|---|---|
| Rama activa | `feature/migracion-api-key` — pusheada a `origin` |
| Commit del fix | `41a1f86` |
| **Producción** | Rama `feature/migracion-api-key`, **modo API Key**, reiniciada 13:56:20 UTC |
| `.env` de producción | `BIMS_URL=in.bims.app` · **`BIMS_FALLBACK_URL` comentada** |
| Tests | **88/88 en verde**; `core/bims.py` compila en el Python 3.7.17 del servidor |
| Validación contra la API viva | ✅ 5 requests consecutivos, jar vacío |
| Validación con órdenes reales | ⏳ **pendiente** — el lunes no hay ventas |

---

## 1. La causa del corte del viernes: una cookie

**Síntoma reportado:** los pedidos no llegaban a BIMS y no se generaban facturas.

**El error real, en el host correcto:**

```
BIMS denegó el acceso a https://in.bims.app/api/contacts/ con API Key:
Session ID no coincide con la cookie de sesión activa. (code 401)
```

**Causa raíz:** `81eb9ba` cambió los 10 métodos de `requests.get/post` (sin estado) a
`self.session.get/post` para ganar keep-alive, y con eso heredó un **cookie jar que nunca fue
intencional**. BIMS devuelve una cookie de sesión; desde el segundo request esa cookie viaja junto al
header `X-API-Key` y BIMS rechaza la combinación con `code: 401`. Como en modo API Key un 401 es
`BimsBusinessError` **terminal**, la orden muere sin reintento.

**La firma que lo prueba — 3 workers, 3 éxitos, después nada.** `gunicorn --workers 3`, cada worker con
su propio singleton `bims = BimsApi()` y su propio jar. El primer request de cada worker sale con jar
vacío:

| Orden | Hora (log, UTC−3) | Resultado |
|---|---|---|
| 200350 | 16:02 | ✅ Sale 31235 ← *la orden "de validación" del viernes* |
| 200353 | 16:04 | ✅ Sale 31236 |
| 200359 | 16:57 | ✅ Sale 31237 |
| 200361 | 17:00 | ❌ 401 cookie |
| 200365 | 17:04 | ❌ 401 cookie |
| 200373 | 17:51 | ❌ 401 cookie |

**Alcance real: 3 órdenes, todas recuperadas.** El reproceso las levantó el sábado 03:13–03:14 →
Sales **31244, 31245, 31246**. Cero facturas perdidas. El sábado 22 hubo 18 ventas y 0 errores.

**Corrección de la cronología:** el rollback a `main` se hizo el **viernes 21:53 UTC**, no el sábado.
La ventana rota fue de ~1 hora; cuando llegó el reporte ya estaba revertido.

> **La hipótesis que se descartó:** se sospechó primero de la conmutación *sticky* de
> `_alternate_base_url` hacia un host donde la key es inválida. La evidencia la mató: el 401 venía de
> `in.bims.app`, sin ninguna conmutación en el log. **El riesgo igual existe** — ver §4.

---

## 2. Por qué la validación del viernes no lo detectó

Dos razones, y las dos son lecciones de método:

1. **Se validó con una sola orden**, inmediatamente después del reinicio: jar vacío, siempre pasa. Una
   orden nunca alcanza para validar un cambio de transporte **con estado**. El mínimo es
   `workers + 1`, porque recién ahí un worker atiende su segunda orden.
2. **El deploy escalonado no podía agarrarlo.** La etapa con `BIMS_API_KEY` vacía ejercitó
   `self.session` en **modo sesión**, donde el `?sid=` y la cookie coinciden. Solo rompe
   **API Key + cookie**.

> **Lección de test más importante de la sesión:** los 6 tests de `81eb9ba` pasaban con el bug puesto
> porque parchean **`requests.Session.send`**, y la extracción de cookies vive **dentro** de
> `Session.send`. Parchear ahí saltea el cookie jar por completo. Para cualquier test de transporte
> HTTP hay que interceptar **`HTTPAdapter.send`**, un nivel más abajo.

---

## 3. El fix (`41a1f86`)

**3 líneas de código productivo.** `_BlockAllCookies(cookiejar.DefaultCookiePolicy)` con `set_ok` y
`return_ok` en `False`, aplicada con `self.session.cookies.set_policy(...)` en el `__init__`.

Va en **los dos modos**, a propósito: la `Session` se adoptó solo por el keep-alive, y hasta `81eb9ba`
los métodos usaban `requests` pelado —sin jar—, que es como `main` factura desde meses. "Sin cookies"
es la configuración probada, no una nueva. La autenticación viaja siempre explícita (header o `?sid=`),
así que ninguna cookie sostiene nada.

**3 tests nuevos**, escritos primero y vistos fallar con el síntoma exacto de producción
(`'Cookie' unexpectedly found in {..., 'X-API-Key': ..., 'Cookie': 'CAKEPHP=...'}`): que no se guarde
la cookie, que el segundo request no la reenvíe, y que el modo sesión tampoco la acarree.

**Validación contra la API viva**, 5 `get_contacts` consecutivos sobre una misma instancia:

```
sid: None | header X-API-Key: True
1 ok | cookies en el jar: 0     ← acá moría el viernes
2..5 ok | cookies en el jar: 0
```

---

## 4. El deploy de hoy, y el tercer agujero cerrado sin código

Se aprovechó la trampa del `.env` a favor: se dejó la rama en disco **sin reiniciar**, así que
producción siguió facturando con `main` mientras se validaba el código nuevo en un proceso aparte.
Recién con las 5 respuestas `ok` se reinició.

**El sticky fallback se cerró desde el `.env`, sin tocar código.** Como la API Key **solo es válida en
`in.bims.app`**, el fallback a `bims.app` no puede ayudar, solo dañar: un hipo transitorio conmutaba el
worker de forma *sticky* a un host donde la key da 401 terminal, y no volvía nunca. Con
`BIMS_FALLBACK_URL` comentada, `_alternate_base_url` corta en su primera guarda y **nunca llega a
mutar `self.base_url`**: la conmutación deja de ser improbable y pasa a ser inalcanzable.

El costo: ante un hipo real, la orden falla tras los 5 reintentos en vez de conmutar. Queda en
`FailedOrder`, Sentry lo recibe (`event_level=logging.ERROR`) y el reproceso la levanta. Ese camino ya
estaba cubierto por `test_sin_secundaria_configurada_mantiene_comportamiento_actual`.

**Pendiente:** el hazard sigue en el código para quien vuelva a poblar la variable. Falta la guarda
por código (no conmutar cuando hay API Key) o, como mínimo, la advertencia en `.env.example`.

---

## 5. Para mañana

1. **Validar con 4+ órdenes facturables** (`workers + 1`). Buscar en `bims_api.log`: cero
   `Caller: login`, cero `BIMS FALLBACK`, cero reintentos, tiempos cerca de **9 s** (no 42).
2. **Recién entonces, mergear la rama a `main`** y devolver producción a `main`. El orden importa:
   `checkout main` **antes** de borrar la rama.
3. **`crontab -l` de root** — ver §6, es lo más urgente de los hallazgos.
4. **`.env.example`**: documentar que `BIMS_FALLBACK_URL` es peligrosa en modo API Key.
5. **Las tres preguntas para BIMS:** host canónico después del 30/09; el `openapi.json` documenta un
   header que no funciona; y por qué `X-API-Key` + cookie de sesión da 401 en vez de que gane la key.

---

## 6. Hallazgos laterales

- **⚠️ `runretryfaileds.sh` está roto y algo no documentado hace su trabajo.** El script hace
  `cd /var/www/integrador.muci.org/backend`, ruta que **no existe** en el servidor: el `cd` falla, el
  `source .venv/bin/activate` falla y `python manage.py retryfaileds` corre sin `manage.py` a la vista.
  Pero el reproceso del sábado **sí ocurrió** (lote secuencial 03:13:57 / 03:14:26 / 03:14:39). Hoy la
  recuperación de facturas depende de un mecanismo invisible, con un script en el repo que *parece* ser
  el responsable y no lo es. **Revisar `crontab -l` de root.**
- **Trampa de husos al leer logs:** el shell del servidor está en **UTC**, pero Django corre con
  `TIME_ZONE = "America/Asuncion"` (**UTC−3**), así que los timestamps *dentro* de los `.log` están 3 h
  atrás. Cruzar `ActiveEnterTimestamp` (UTC) contra el contenido de un log sin restar 3 h lleva a
  conclusiones falsas — pasó en esta sesión.
- **La rotación de `bims_api.log` se comió la evidencia del viernes.** Rota por tamaño con
  `backupCount=3` (~3 MB); una corrida de `get_contacts` paginando 18.000 contactos escribe ~10 MB y
  **quema las 4 ventanas en un minuto** (pasó el 23/08 a las 21:03). El histórico largo de órdenes
  sobrevive solo en `bims_sync.log`. Vale bajarle el nivel de log a ese dump.
- **Nombres deformados en el reproceso:** las 3 órdenes recuperadas crearon contactos nuevos
  (18140–18142) con nombres tipo `C L A R I C E C O M P A S S O O M P A S S O` — letras espaciadas y un
  fragmento duplicado. Bug independiente, sin diagnosticar, y ensucia BIMS con duplicados.
- **Acceso SSH:** la vía vieja (`root@159.89.228.18` con `anthropic_readonly_muciserver`) **dejó de
  funcionar**; el server rechaza la clave. La vigente es
  `ssh -i ~/.ssh/muci anthropic_readonly@muci.org`. Ese usuario **no puede leer el `.env`** ni
  `journalctl`, y `git` necesita `-c safe.directory=/var/www/integrador`.
- **El push pelado funciona:** `git push origin <rama>` entró sin PAT ni credential helper efímero.
  Antes se creía necesario armar el helper a mano.
- **`.env.local` no estaba en `.gitignore`** (la regla era `.env` exacto). Corregido con `.env.*` +
  `!.env.example`. Tenía `SECRET_KEY`, `DB_PASSWORD`, `WOOCOMMERCE_SECRET`, `BIMS_PASSWORD`,
  `POS_LOOKUP_TOKEN` y la `BIMS_API_KEY`; un `git add -A` las publicaba.
- Sigue vivo: **PAT sin revocar**, por decisión explícita de Carlos.
- Sigue sin revisar: **201 `FailedOrder` en FAILED**, backlog viejo.
