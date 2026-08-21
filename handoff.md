# Handoff — sesión del 2026-08-21

> Sesión larga y de tres actos. Se fue a confirmar un pendiente que **no existía**; se encontró y
> **arregló en producción** un problema que llevaba semanas sangrando en silencio; y apareció un
> deadline externo de 40 días que se convirtió en el trabajo del día. Hay código nuevo, en rama.

## Estado al cierre

| | |
|---|---|
| Rama activa | `feature/migracion-api-key` (1 commit de código: `81eb9ba`) |
| Producción | Rama `main`, HEAD `370e99a`, servicio `active` — **corriendo con el fix del host aplicado** |
| Tests | **85/85 en verde**; `core/bims.py` compila en el Python 3.7.17 del servidor |
| Deadline externo | **30/09/2026**, BIMS corta usuario+password |
| Hub → CRM | Pausado por decisión de Carlos |

---

## 1. El deploy ya estaba hecho — los handoffs del 18 y 19 estaban equivocados

Ambos decían que producción seguía en `08c7f7c` sobre la rama vieja y que el pase a `main` estaba
bloqueado por falta de acceso SSH. **Las dos afirmaciones eran falsas.**

- **El SSH funciona.** `anthropic_readonly_muciserver` conecta bien contra `root@159.89.228.18`.
- **El checkout a `main` se hizo el 18/08 a las 13:04 UTC**, según el reflog del servidor — antes de
  que se escribiera el handoff que lo declaraba pendiente.
- Los 2 commits que faltaban tocan **solo `handoff.md`**: cero código, cero migraciones.
- `bims_api.log` nunca estuvo en riesgo: `e53b849` ya lo había destrackeado. El `cp` de respaldo que
  el handoff marcaba como "no opcional" nunca se corrió y no hizo falta.

---

## 2. ⚠️ El 401 de producción: causa raíz encontrada y ARREGLADA

**Síntoma:** cada venta a `https://bims.app/api/sales/` devolvía HTTP 200 con `code: 401`, se
reintentaba 5 veces, conmutaba al fallback `in.bims.app` y ahí facturaba bien. ~25 s de latencia
inútil por orden, 105 ocurrencias en 7 días. Enmascarado: las ventas no fallaban, solo tardaban, y
todo se logueaba en `WARNING`, así que no disparaba alertas.

**Causa raíz — no era lo que parecía.** No es un login roto ni un host degradado:

| | `bims.app` | `in.bims.app` |
|---|---|---|
| Login usuario+password | **"Credenciales incorrectas (CODE 003)"** | ✅ ok, devuelve `sid` |
| API Key | 401 `Unauthorized` | ✅ ok en `/contacts/`, `/sales/`, `/products/` |

Dos mecanismos de auth independientes fallan en uno y funcionan en el otro: **la cuenta `apimuci` /
tenant `muci` vive en `in.bims.app`**. `BIMS_URL` apuntaba al host equivocado, y el fallback lo venía
tapando en silencio desde que se agregó el 08/07.

**Arreglado en producción**, sin tocar código: `BIMS_URL=https://in.bims.app/api` y
`BIMS_FALLBACK_URL=https://bims.app/api`, más `systemctl restart` a las 17:48:10 UTC.
**Verificado: cero ocurrencias de "No dispone de permisos" desde el reinicio.**

> **Trampa que costó tiempo:** editar el `.env` no alcanza. `settings.py` lo lee con `dotenv_values()`
> en el import, así que los workers vivos siguen con los valores viejos hasta el reinicio. El `.env`
> quedó modificado 2h43m antes de que el servicio se reiniciara.

El análisis del mecanismo interno (por qué un `login()` fallido se degradaba a "reintentar 5 veces")
sigue siendo válido y está en la memoria `project_bims_login_primario_falla`. Pero no hace falta
parchearlo: la migración de la sección 3 elimina ese camino de código entero.

---

## 3. Lo urgente: BIMS corta usuario+password el 30/09/2026

Mensaje directo de BIMS a Carlos: *"El 30 de Septiembre dejará de funcionar el método de autenticación
con usuario y contraseña para el API de BIMS, en su lugar, se utilizará un API Key."* Ya no es una
fecha inferida. **Quedan 40 días.**

El análisis previo vive fuera del repo, en `/home/vallory/IA/bims` (rama `feature/consultas-prod`).

### El formato del header: verificado contra la API viva

La key **va cruda, sin prefijo de tenant**. `BIMS_TENANT` no se usa para la API Key (sí sigue siendo
necesario para el login por sesión). La API es permisiva: acepta `X-API-Key`, `X-Api-Key`,
`Authorization: Bearer <key>`, `Authorization: <key>` y hasta `apikey`.

**El `openapi.json` está mal.** Su único `securitySchemes` documenta
`Authorization: Bearer {tenant}_{api_key}` — que devuelve 401 en los dos hosts. La doc publicada en
`ayuda.bims.app/api` es la correcta y muestra las tres formas con la key cruda.

> **Regla que salió de acá:** ante conflicto, **la doc publicada gana sobre el `openapi.json` bajado.**
> En esta sesión se le creyó primero al JSON y se "corrigieron" dos puntos del plan que estaban bien.

Otros datos: `User.api_key` **no viene** en el login de ningún host, así que la key solo puede venir del
`.env`. Y la key es **self-service**: se activa en la configuración del usuario en BIMS, sin esperar a
un administrador — lo que el handoff de `IA/bims` temía que bloqueara el plazo.

### Lo implementado (commit `81eb9ba`)

Modo **dual**, gobernado por la presencia de `BIMS_API_KEY`:

- `__init__` crea la `session` **antes** de ramificar (antes se creaba después del login). Con key:
  header `X-API-Key` y `sid = None`, sin login. Sin key: el `login()` de siempre.
- `_request_with_relogin`: con key, un 401 es `BimsBusinessError` terminal — sin relogin, sin
  reintentos, sin conmutación de host. **El bucle de 5 reintentos desaparece por construcción.**
- Los 10 métodos pasan a `self.session.get/post` con el `sid` condicional.
- `find_razon_social_by_ruc` queda **deliberadamente** en `requests.get`: turuc es un tercero y la
  sesión lleva la credencial de BIMS. Hay un test que falla si alguien hace ese cambio — se verificó
  inyectando el error a propósito, y atrapó el `X-API-Key` viajando a turuc.
- `BIMS_API_KEY` en `settings.py`, `test_settings.py` (vacía) y `.env.example`.

Seis tests nuevos que asertan sobre la request realmente preparada (interceptan
`requests.Session.send` y miran URL y headers), no sobre mocks.

---

## 4. Para retomar: el deploy escalonado

Carlos eligió **no** probar la escritura con un re-POST idempotente, y desplegar en dos pasos. El
modo de falla así es un `FailedOrder` recuperable, nunca una factura duplicada.

> **OJO:** el `.env` de producción **ya tiene `BIMS_API_KEY` puesta** (se cargó para verificar el
> formato). Desplegar el código tal cual **cambia la autenticación en el mismo acto**. Para escalonar
> hay que vaciarla primero.

```bash
# Paso 1 — deploy en modo sesion (comportamiento identico al de hoy)
cd /var/www/integrador
# vaciar BIMS_API_KEY en el .env  (dejar BIMS_API_KEY= )
git fetch origin --prune
git checkout feature/migracion-api-key && git pull origin feature/migracion-api-key
/root/.local/share/virtualenvs/integrador-ObaHlHmv/bin/python manage.py migrate
systemctl restart mucintegrador.service
# confirmar que una venta real entra normal

# Paso 2 — prender la API Key y mirar de cerca la primera venta
# poner el valor real en BIMS_API_KEY del .env
systemctl restart mucintegrador.service
journalctl -u mucintegrador.service -f
```

**Rollback:** vaciar `BIMS_API_KEY` y reiniciar. Sin redeploy.

**Falta verificar:** una escritura real (`create_sale`) con API Key. La lectura está confirmada en
`/contacts/`, `/sales/` y `/products/`. Los permisos **no** son el problema: el grupo `API` (id 4)
trae `salesAdd`, `salesSend`, `contactsAdd` y `contactsEdit`, y `/api/sales/add.json` documenta
`application/json`, que es lo que el integrador manda.

### Dos preguntas para BIMS

1. **¿Cuál es el host canónico del tenant después del 30/09?** Hoy solo `in.bims.app` responde.
2. El `openapi.json` publicado documenta un formato de header que no funciona.

### Hallazgos laterales anotados

- **BIMS ahora documenta la idempotencia** de `/sales/add.json` ("reintento idempotente", `Sale._id`
  estable al reintentar). Eso le baja mucho el sentido a `project_deteccion_venta_duplicada`, que
  existía para cubrir el riesgo de que fuera comportamiento indocumentado. Revisar antes de retomarlo.
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
