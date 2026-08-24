# Handoff — sesión del 2026-08-24

> Sesión larga y de cuatro actos. Se diagnosticó el corte de facturación del viernes —**descartando una
> primera hipótesis con evidencia**—, se arregló, se validó contra la API viva y se desplegó. Se
> construyó y desplegó la gestión de sucursales, que era el pedido nuevo del día. Y al final, tirando
> del hilo de "¿por qué el servicio se reinicia solo?", se encontró y arregló la causa de los cuelgues
> que el integrador tenía hasta mediados de año.

## Estado al cierre

| | |
|---|---|
| **Producción** | `feature/gestion-sucursales` (`bfdad62`), **modo API Key**, reiniciada 19:21 UTC |
| `.env` de producción | `BIMS_URL=in.bims.app` · **`BIMS_FALLBACK_URL` comentada** |
| BD de producción | tabla `core_sucursal` creada y sembrada (migraciones 0006/0007 aplicadas) |
| Rama con todo | `feature/timeouts-bims` (`ffef255`) — **sin desplegar** |
| `main` | `0e87826` — **sin tocar en todo el día** |
| Tests | **122/122** en verde en la punta |
| Validación pendiente | **4+ órdenes facturables**; al cierre pasó **una** |

### Las cuatro ramas y cómo se contienen

```
main 0e87826
 ├── feature/migracion-api-key  1754190   (fix del cookie + handoff)
 └── feature/gestion-sucursales bfdad62   ← EN PRODUCCIÓN (mergeó api-key adentro)
      └── feature/timeouts-bims ffef255   ← la punta, contiene todo
```

`feature/timeouts-bims` contiene absolutamente todo lo del día, así que desplegarla no pierde nada.

---

## 1. El corte del viernes: era una cookie

**Síntoma:** los pedidos no llegaban a BIMS. **Error real, en el host correcto:**

```
BIMS denegó el acceso a https://in.bims.app/api/contacts/ con API Key:
Session ID no coincide con la cookie de sesión activa. (code 401)
```

**Causa raíz:** `81eb9ba` cambió los 10 métodos de `requests.get/post` (sin estado) a
`self.session.get/post` para ganar keep-alive, y heredó un **cookie jar que nunca fue intencional**.
BIMS devuelve una cookie de sesión; desde el segundo request esa cookie viaja junto al header
`X-API-Key` y BIMS rechaza la combinación. Como en modo API Key un 401 es terminal, la orden muere sin
reintento.

**La firma que lo prueba — 3 workers, 3 éxitos, después nada.** Cada worker tiene su propio singleton
`bims = BimsApi()` y su propio jar; el primer request de cada uno sale con jar vacío:

| Orden | Hora (log) | Resultado |
|---|---|---|
| 200350 | 16:02 | ✅ Sale 31235 ← *la orden "de validación" del viernes* |
| 200353 | 16:04 | ✅ Sale 31236 |
| 200359 | 16:57 | ✅ Sale 31237 |
| 200361 / 200365 / 200373 | 17:00 / 17:04 / 17:51 | ❌ 401 cookie |

**Alcance real: 3 órdenes, todas recuperadas** el sábado → Sales 31244, 31245, 31246. El rollback a
`main` se hizo el **viernes 21:53 UTC**, no el sábado: la ventana rota fue de ~1 hora y ya estaba
revertida cuando llegó el reporte.

> **La hipótesis que se descartó:** se sospechó primero de la conmutación *sticky* de
> `_alternate_base_url` hacia un host donde la key es inválida. La evidencia la mató — el 401 venía de
> `in.bims.app`, sin conmutación alguna. El riesgo igual era real y **se cerró desde el `.env`**
> comentando `BIMS_FALLBACK_URL`: sin secundaria, `_alternate_base_url` corta en su primera guarda y
> nunca llega a mutar `self.base_url`.

### Por qué la validación del viernes no lo detectó

1. **Se validó con una sola orden**, justo después del reinicio: jar vacío, siempre pasa. Una orden no
   alcanza para validar un cambio de transporte **con estado**. El mínimo es `workers + 1`.
2. **El deploy escalonado no podía agarrarlo:** la etapa con `BIMS_API_KEY` vacía ejercitó
   `self.session` en modo sesión, donde el `?sid=` y la cookie coinciden. Solo rompe API Key + cookie.

> **Lección de test más importante del día:** los 6 tests de `81eb9ba` pasaban con el bug puesto porque
> parchean **`requests.Session.send`**, y la extracción de cookies vive **dentro** de `Session.send`.
> Para cualquier test de transporte HTTP hay que interceptar **`HTTPAdapter.send`**, un nivel más abajo.
> Lo mismo aplica a `timeout`, que tampoco viaja en la `PreparedRequest`.

**El fix (`41a1f86`):** `_BlockAllCookies` con `set_ok`/`return_ok` en `False`, aplicada en el
`__init__` para **los dos modos** — la `Session` se adoptó solo por el keep-alive y el jar nunca se
quiso. Validado contra la API viva con 5 `get_contacts` seguidos sobre una misma instancia: `sid: None`,
header presente, 5× `ok` con jar vacío.

---

## 2. Gestión de sucursales (`f45d7f0` + `bfdad62`)

El mapeo cajero POS → punto de venta vivía hardcodeado en `POS_USER_ID_TO_POSALE`. Ahora es la tabla
`Sucursal`, editable desde el Django admin.

**Tres tipos de fila:** los `cajero` apuntan a un usuario concreto de WordPress; `pos_sin_mapeo` y `web`
son **reglas por defecto** sin usuario ni email, de fila única (lo garantiza `clean()`).
**`bims_posale_id` vacío significa NO FACTURAR** — eso reemplaza el `if user_id_value == 2` que estaba
hardcodeado y permite dar de baja cualquier cajero sin borrarlo.

**Las constantes quedan como red de seguridad:** si la tabla está vacía o la consulta falla, se usan
ellas y se loguea el desvío. Una tabla nueva no puede tener el poder de frenar la facturación.

**El alta resuelve datos contra WooCommerce:** cargás el email y `save_model` trae el `wp_user_id` (o al
revés). Los cajeros POS **son "customers" de `wc/v3`** con rol `fooeventspos_cashier` — no hace falta la
API de WordPress. Ojo: **`role=all` es obligatorio** al buscar, o `wc/v3` solo devuelve los `customer`.

**Y el punto de venta se elige de una lista traída de BIMS**, no se escribe. Escribirlo a mano permitía
cargar un ID inexistente cuyo error aparecía recién al facturar. Sin caché: se consulta al abrir el
formulario, así que tarda unos segundos; si BIMS no responde, degrada al campo numérico con un aviso.

> **Detalle no obvio:** `core/sucursales.py` importa `core.bims` **dentro** de la función, no arriba.
> Ese módulo instancia `BimsApi()` en el import y en modo sesión eso hace login; importarlo arriba haría
> que **abrir el admin dependiera de que BIMS esté arriba**.

### Quién define el `posale_id`

| Sistema | Dueño de | Ejemplo |
|---|---|---|
| WordPress / WooCommerce | el usuario cajero: id, email, rol | `729`, sancosmos@muci.org, `fooeventspos_cashier` |
| FooEvents POS | qué cajero hizo la venta | `_fooeventspos_user_id: "729"` |
| **BIMS** | **el punto de venta y su id** | `4` = Caja San Cosmos |
| El integrador | la traducción | `729 → 4` |

Los 4 puntos de venta que tiene BIMS (`GET /posales/`, ids como **strings**):

| ID | Nombre en BIMS | `bill_code` |
|---|---|---|
| 1 | Caja Tatakualab | 001 |
| 4 | Caja San Cosmos | 002 |
| 6 | Caja WEB | 003 |
| 7 | **Caja Fund MuCi** | 004 |

**El `7` no es un cajón genérico:** es un punto de venta real. Las ventas de cajeros no registrados se
facturan a nombre de Fund MuCi. Y **no hay ninguno libre** — para una sucursal nueva hay que crear el
punto de venta en BIMS primero (`POST /api/posales/add.json`).

**Varios cajeros pueden compartir un punto de venta** y el modelo ya lo soportaba: `wp_user_id` es
`unique`, `bims_posale_id` no.

**Verificado en producción sin desplegar**, con un worktree aparte + symlink al `.env`: 6 de 6, con
resolución `729→4`, `3→1`, `2→None`, `99999→7`, `web→6` y las dos direcciones de WooCommerce contra la
tienda real.

---

## 3. Timeouts y presupuesto de reintentos (`ffef255`, sin desplegar)

Tirando del hilo "¿por qué hay un cron que reinicia el servicio cada 6 horas?", salió esto:

**Solo `login()` tenía timeout.** Las otras 12 llamadas usaban `self.session.get/post` sin ninguno, así
que un BIMS que acepta la conexión y no responde bloqueaba un worker **sin límite**. Con `--workers 3`,
tres de esas y el integrador deja de atender. (`core/ruc.py` sí tenía `timeout=5`, y el `CLAUDE.md` lo
exige: era un olvido, no una decisión.)

**Y un timeout por request no alcanzaba.** 5 intentos × 30 s + la conmutación de host ≈ **316 s**,
contra los **`--timeout 120`** de gunicorn. Al pasarse, gunicorn mata al worker **por señal**, y un
worker matado por señal **no ejecuta el `except` que graba el `FailedOrder`**: la orden desaparece sin
factura y sin registro.

Lo implementado: timeout inyectado en `_request_with_relogin` (el embudo de las 12 llamadas, una línea
en vez de 12 ediciones), conexión 5 s / lectura 30 s, y un **presupuesto de 40 s por llamada** con tres
detalles que lo hacen real — no se arranca un intento nuevo si se agotó; el timeout de lectura **se
recorta al restante** o el presupuesto sería decorativo; y la conmutación de host **comparte** el
presupuesto. WooCommerce pasa de 480 s a 30 s. El presupuesto es por **llamada**, no por orden: uno por
orden habría que pasarlo desde la vista y toca `services.py` y `views.py`.

### La historia de los cuelgues

Carlos: el integrador se colgaba seguido hasta ~mayo–julio y "si nadie lo reiniciaba quedaba colgado
para siempre". Sospechaba de productos sin SKU o precio 0 — **falso**, esos son caminos en memoria que
terminan en un `ValueError` → 400 en milisegundos. Los tres commits que atacaron la zona real:

| Fecha | Commit | Qué arregló |
|---|---|---|
| 13/05 | `b3cf2aa` | `res.json()` sin proteger → `ValueError` no capturado ante un 502/504 en HTML |
| 29/06 | `e7a9911` | 401/403 permanente se reintentaba 5 veces, con relogin en cada vuelta |
| 08/07 | `4fb3524` | **le puso `timeout=30` al login** |

**El tercero explica el "para siempre":** `bims = BimsApi()` llama a `login()` **en el import del
módulo**, o sea durante el arranque del worker. Sin timeout, el worker se bloqueaba antes de entrar al
loop; gunicorn lo mataba, levantaba otro, y el nuevo se bloqueaba igual. Servicio arriba, ningún worker
capaz de atender. `ffef255` cierra la pieza que faltaba: las llamadas de facturación.

---

## 4. Para mañana

1. **Cerrar la validación: 4+ órdenes facturables.** Al cierre pasó **una** (200707 → Sale 31264,
   `Params: {}` sin `sid`, 8,55 s, `posale_id: 4` resuelto de la BD). Una sola no prueba nada — es
   exactamente la trampa del viernes. Buscar en `bims_api.log`: cero `Caller: login`, cero
   `BIMS FALLBACK`, cero reintentos, tiempos cerca de 8-9 s.
2. **Después: mergear a `main`.** El orden natural es `timeouts-bims` → `main` (contiene todo), y
   limpiar las otras tres ramas. `checkout main` **antes** de borrar cualquier rama.
3. **Desplegar `ffef255`** una vez que la validación cierre. Sin migraciones nuevas propias.
4. **Renombrar la fila del `7`** a `Caja Fund MuCi` desde el admin. Hoy dice "Cualquier otro cajero POS",
   que oculta que esas ventas se facturan a Fund MuCi. Es dato de Carlos, no se cambió por código.
5. **Decidir el agujero de las órdenes fallidas** — ver abajo, es lo más importante que queda abierto.

---

## 5. Hallazgos laterales

- **⚠️ NO existe reproceso automático de órdenes fallidas.** El crontab de root **no tiene nada que
  corra `retryfaileds`**, y `runretryfaileds.sh` no está referenciado en ningún lado: es código muerto
  además de roto (hace `cd` a una ruta inexistente). Lo único que reintenta es `sync_bims_contacts`, y
  **solo** órdenes con `message__startswith="Pausada: Esperando"`. **Corrección a lo que se afirmó
  durante la sesión:** las órdenes fallidas *no* "se recuperan en el reproceso" — hoy quedan falladas
  hasta que un humano entre al admin. Eso explica las **201 en FAILED** acumuladas. Las 3 del viernes
  probablemente se recuperaron con el botón del admin; **sin confirmar con Carlos**.
- **El cron que quema los logs:** `0 0 * * * ... sync_bims_contacts` pagina 18.000 contactos y cada
  llamada escribe en `bims_api.log`, que rota por tamaño con `backupCount=3`. **Quema las 4 ventanas en
  un minuto** — eso borró la evidencia del viernes antes de poder leerla. El histórico largo sobrevive
  solo en `bims_sync.log`.
- **Reinicio automático cada 6 horas** (`0 */6`, o sea 00/06/12/18 UTC, **4 veces al día**). Es un parche
  al síntoma de los cuelgues. Con `ffef255` estable se puede discutir sacarlo — hoy corta una
  facturación por la mitad 4 veces al día. **Y invalida una suposición usada dos veces hoy:** dejar
  código nuevo en disco sin reiniciar **no es un estado estable**, aguanta como máximo ~6 h. Para
  validar sin activar, usar un **worktree aparte**.
- **Trampa de husos:** el shell del servidor está en **UTC**, pero Django corre con
  `TIME_ZONE = "America/Asuncion"` (**UTC−3**), así que los timestamps *dentro* de los `.log` están 3 h
  atrás. Cruzar `ActiveEnterTimestamp` contra el contenido de un log sin restar 3 h lleva a conclusiones
  falsas — pasó en esta sesión.
- **Acceso SSH:** la vía vieja (`root@159.89.228.18` con `anthropic_readonly_muciserver`) **dejó de
  funcionar**. La vigente es `ssh -i ~/.ssh/muci anthropic_readonly@muci.org`. Ese usuario **no puede
  leer el `.env`** ni `journalctl`, y `git` necesita `-c safe.directory=/var/www/integrador`.
- **Comandos multilínea con heredoc se rompen al pegarlos** en la terminal: dos espacios de indentación
  invalidan el terminador (`PY`/`OUTER` debe ir en columna 0), y las rutas largas hacen que la línea se
  corte y se pierda el `<`. Lo que funciona: `scp` de un script corto + un `ssh` de una línea de ~115
  caracteres o menos.
- **`.env.local` no estaba en `.gitignore`** (la regla era `.env` exacto). Corregido con `.env.*` +
  `!.env.example`. Tenía `SECRET_KEY`, `DB_PASSWORD`, `WOOCOMMERCE_SECRET`, `BIMS_PASSWORD`,
  `POS_LOOKUP_TOKEN` y la `BIMS_API_KEY`.
- **El push pelado funciona:** `git push origin <rama>` entra sin PAT ni credential helper efímero.
- **Nombres deformados en el reproceso:** las 3 órdenes recuperadas crearon contactos nuevos
  (18140–18142) con nombres tipo `C L A R I C E C O M P A S S O O M P A S S O`. Bug independiente, sin
  diagnosticar, y ensucia BIMS con duplicados.
- **Quirk de logging preexistente:** el logger `core` tiene `propagate: True` y sus mismos handlers están
  en el root, así que todo `core.*` sale **duplicado en stdout** (no en `bims_sync.log`, verificado).
- **Tres preguntas para BIMS:** ¿host canónico del tenant después del 30/09? El `openapi.json` documenta
  un formato de header que no funciona. Y ¿por qué `X-API-Key` + cookie de sesión devuelve 401 en vez de
  que gane la key?
- **Decisión abierta:** si las reglas `web` y `pos_sin_mapeo` deberían poder quedar sin punto de venta.
  Hoy se pueden vaciar, y eso significa que esas órdenes no se facturan.
- Sigue vivo de antes: **PAT sin revocar**, por decisión explícita de Carlos.
