# Handoff — sesión del 2026-08-21

> Sesión de tres actos. Se fue a confirmar un pendiente que **no existía**; se encontró y **arregló**
> un problema que llevaba semanas sangrando en silencio en producción; y apareció un deadline externo
> de 40 días que terminó **implementado, desplegado y validado el mismo día**.

## Estado al cierre

| | |
|---|---|
| Rama activa | `feature/migracion-api-key` — pusheada a `origin` |
| Commits de la sesión | `81eb9ba` (código), `fdc9b13` (handoff), + este |
| **Producción** | Rama `feature/migracion-api-key`, **modo API Key activo**, `BIMS_URL=in.bims.app` |
| Tests | **85/85 en verde**; `core/bims.py` compila en el Python 3.7.17 del servidor |
| Deadline BIMS 30/09/2026 | ✅ **cumplido 40 días antes** |
| Hub → CRM | Pausado por decisión de Carlos |

---

## 1. El deploy ya estaba hecho — los handoffs del 18 y 19 estaban equivocados

Ambos decían que producción seguía en `08c7f7c` sobre la rama vieja y que el pase a `main` estaba
bloqueado por falta de acceso SSH. **Las dos afirmaciones eran falsas.**

- **El SSH funciona.** `anthropic_readonly_muciserver` conecta bien contra `root@159.89.228.18`.
- **El checkout a `main` se hizo el 18/08 a las 13:04 UTC**, según el reflog del servidor — antes de
  que se escribiera el handoff que lo declaraba pendiente.
- Los 2 commits que faltaban tocaban **solo `handoff.md`**: cero código, cero migraciones.
- `bims_api.log` nunca estuvo en riesgo: `e53b849` ya lo había destrackeado.

---

## 2. ✅ El 401 de producción: causa raíz encontrada y ARREGLADA

**Síntoma:** cada venta a `https://bims.app/api/sales/` devolvía HTTP 200 con `code: 401`, se
reintentaba 5 veces, conmutaba al fallback `in.bims.app` y ahí facturaba. ~25 s de latencia inútil por
orden, 105 ocurrencias en 7 días. Enmascarado: las ventas no fallaban, solo tardaban, y todo era
`WARNING`, así que no disparaba alertas.

**Causa raíz — no era lo que parecía:**

| | `bims.app` | `in.bims.app` |
|---|---|---|
| Login usuario+password | **"Credenciales incorrectas (CODE 003)"** | ✅ ok, devuelve `sid` |
| API Key | 401 `Unauthorized` | ✅ ok |

Dos mecanismos de auth independientes fallan en uno y funcionan en el otro: **la cuenta `apimuci` /
tenant `muci` vive en `in.bims.app`**. `BIMS_URL` apuntaba al host equivocado y el fallback lo venía
tapando en silencio desde el 08/07.

**Arreglado** invirtiendo `BIMS_URL` / `BIMS_FALLBACK_URL` + reinicio. Verificado: **cero ocurrencias**
desde entonces.

> **Trampa que costó tiempo:** editar el `.env` no alcanza. `settings.py` lo lee con `dotenv_values()`
> en el import, así que los workers vivos siguen con los valores viejos hasta el reinicio.

---

## 3. La migración a API Key: implementada y validada

BIMS avisó por mensaje directo: **el 30/09/2026 deja de funcionar la autenticación usuario+password.**
El análisis previo vive fuera del repo, en `/home/vallory/IA/bims`.

### El formato del header

La key **va cruda, sin prefijo de tenant**. `BIMS_TENANT` no se usa para la API Key. La API acepta
`X-API-Key`, `X-Api-Key`, `Authorization: Bearer <key>`, `Authorization: <key>` y `apikey`.

**El `openapi.json` está mal:** su único `securitySchemes` documenta
`Authorization: Bearer {tenant}_{api_key}`, que devuelve 401 en los dos hosts. La doc publicada en
`ayuda.bims.app/api` es la correcta.

> **Regla que salió de acá:** ante conflicto, **la doc publicada gana sobre el `openapi.json` bajado.**
> En esta sesión se le creyó primero al JSON y se "corrigieron" dos puntos del plan que estaban bien.

`User.api_key` **no viene** en el login de ningún host: la key solo puede venir del `.env`. Y es
**self-service**, se activa en la config del usuario en BIMS.

### Lo implementado (`81eb9ba`)

Modo **dual**, gobernado por la presencia de `BIMS_API_KEY`:

- `__init__` crea la `session` **antes** de ramificar. Con key: header `X-API-Key` y `sid = None`, sin
  login. Sin key: el `login()` de siempre.
- `_request_with_relogin`: con key, un 401 es `BimsBusinessError` terminal — sin relogin, sin
  reintentos, sin conmutación. **El bucle de 5 reintentos desaparece por construcción.**
- Los 10 métodos pasan a `self.session.get/post` con el `sid` condicional.
- `find_razon_social_by_ruc` queda **deliberadamente** en `requests.get`: turuc es un tercero y la
  sesión lleva la credencial de BIMS. Hay un test que falla si alguien hace ese cambio — verificado
  inyectando el error a propósito.
- 6 tests nuevos que asertan sobre la request realmente preparada (interceptan `requests.Session.send`
  y miran URL y headers), no sobre mocks.

### El deploy escalonado, y su resultado

Se desplegó en dos pasos para aislar las causas: primero con `BIMS_API_KEY` **vacía** (valida el cambio
de transporte a `self.session` + keep-alive contra la auth conocida), después con la key.

> Vaciar la variable no es cosmético: el código con key vacía **no es inerte**, cambia el transporte.

**Validación end-to-end en modo API Key:**

| | |
|---|---|
| Orden | 200350 |
| **BIMS Sale ID** | **31235** |
| Respuesta a WooCommerce | `POST /sales/` 200 |
| Duración | **~9 s** (vs ~42 s esa mañana con el bug del host) |
| Relogins / conmutaciones / reintentos | **0** |
| Logins tras el arranque | **0** ← la señal de que el modo está activo |

Lectura validada además contra el proceso vivo: `sid=None`, header presente, `get_contacts` →
`status=ok`, 18.000 contactos.

**Rollback:** vaciar `BIMS_API_KEY` + `systemctl restart`. Sin redeploy.

---

## 4. Para mañana

1. **Mergear la rama a `main`** (Carlos decidió no hacerlo el 21) y **devolver producción a `main`**.
   El orden importa: `checkout main` **antes** de borrar la rama, que es justo lo que se invirtió la
   vez pasada. Ojo que el `git pull` pelado en el servidor hoy apunta a la rama, no a `main`.
2. **Las dos preguntas para BIMS:**
   - ¿Cuál es el host canónico del tenant después del 30/09? Hoy solo `in.bims.app` responde.
   - El `openapi.json` publicado documenta un formato de header que no funciona.
3. **Los 201 `FailedOrder` en FAILED.** No se movieron durante todo el deploy, así que son backlog
   viejo. Vale revisar si son recuperables con `./runretryfaileds.sh` o si es basura acumulada.

### Hallazgos laterales anotados

- **BIMS ahora documenta la idempotencia** de `/sales/add.json` ("reintento idempotente", `Sale._id`
  estable al reintentar). Eso le baja mucho el sentido a `project_deteccion_venta_duplicada`, que
  existía para cubrir el riesgo de que fuera comportamiento indocumentado. **Revisar antes de retomarlo.**
- Dos códigos retryables documentados que el integrador no maneja explícitamente:
  `DEADLOCK_RETRYABLE` (`retry_after_ms=700`) y `SALE_PERSISTENCE_UNCONFIRMED`. Hoy caen en el camino
  transitorio y se reintentan — correcto por accidente, pero ignorando el delay sugerido.
- **Documento con sufijo rompe la consulta RUC:** llega `"1346288 - 1"` y turuc responde 400.
  `ruc.py` es fail-safe, solo se pierde la corrección de razón social. Sin medir cuántos son.
- **Datos de capacidad** (destraban la pregunta sync-vs-async del hub): `nproc` = 4, RAM 7.9 GB,
  gunicorn `--workers 3`, `--timeout 120`.
- Pendiente viejo nunca chequeado: `runretryfaileds.sh` hace `cd /var/www/integrador.muci.org/backend`,
  ruta que no coincide con el servidor.
- Sigue vivo: **PAT sin revocar**, por decisión explícita de Carlos.
