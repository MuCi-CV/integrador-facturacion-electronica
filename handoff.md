# Handoff — sesión del 2026-08-07

## Estado al cierre

| | |
|---|---|
| Rama activa | `feature/deteccion-venta-duplicada` — pusheada (`bb24560`) |
| `main` | mergeada, pusheada y sincronizada con el remoto (`e53b849`) |
| Suite de tests | 59/59 en verde |
| Trabajo pendiente | implementar el plan de detección de facturas duplicadas |

PR listo para abrir en:
`https://github.com/MuCi-CV/integrador-facturacion-electronica/pull/new/feature/deteccion-venta-duplicada`

---

## 1. Merge de `feature/refactor-service-layer` → `main` (terminado)

Fast-forward limpio de 47 commits, sin conflictos. Verificado con: suite completa
(59 tests), `manage.py check` (0 issues), `makemigrations --check` (*No changes
detected*), grafo de migraciones lineal `0001→0005`, y `diff main feature` vacío.

Compatibilidad con producción (Python 3.7.17) confirmada: el código compila bajo
Python 3.6 —cota inferior estricta— y no hay sintaxis moderna (uniones `X | Y`,
`match/case`, walrus, f-strings con `=`, genéricos `list[]`).

También se destrackeó `bims_api.log`, que estaba en el índice pese a que `.gitignore`
ya cubre `*.log`. Sigue en disco. Se auditó antes: no contenía credenciales ni PII
(password enmascarado, `token: null`, emails vacíos) — relevante porque **el repo es
público**.

Push a `MuCi-CV/integrador-facturacion-electronica`: `50c5d45..e53b849`.

## 2. Riesgo de factura duplicada: investigado y redimensionado

La preocupación era que `_retry_request` reenviara el `POST /sales/` tras un timeout y
BIMS emitiera una segunda factura. **No ocurre.**

Análisis del histórico completo de logs (8 archivos, ~14 MB):

```
respuestas de venta parseadas : 334
pedidos distintos (_id)       : 286
respuestas 'ya confirmada'    : 27
duplicados                    : 0
```

48 de esos POSTs fueron reenvíos del mismo pedido — el escenario exacto que se temía.
BIMS **deduplica por el campo `_id`** (el `order_id` de WooCommerce): reconoce el
pedido, se niega a crear una venta nueva y devuelve la original con el mensaje
*"La venta no ha sido editada porque ya se encuentra confirmada en el servidor."*
Ningún `_id` generó más de un `Sale.id`.

`create_sale` ya maneja bien esa respuesta, y el `_id` nunca va vacío (`order_id` es
parámetro obligatorio de `process_order`).

**El riesgo que sí queda** es que esa deduplicación es un comportamiento **no
documentado** de BIMS. Si lo cambian, duplicaríamos facturas en silencio.

## 3. Trabajo pendiente: red de detección

Rama `feature/deteccion-venta-duplicada`, pusheada al remoto. **Nada de código
implementado todavía** — solo la documentación de diseño.

- Spec: `docs/superpowers/specs/2026-08-07-deteccion-venta-duplicada-design.md` (`0652614`)
- Plan: `docs/superpowers/plans/2026-08-07-deteccion-venta-duplicada.md` (`1c32039`)
- Este handoff (`bb24560`)

El plan tiene 4 tareas en TDD, cada una con el test primero, el código exacto y su
commit:

1. `create_sale` pasa a devolver `(sale_id, error, reutilizada)`.
2. Campo `bims_sale_id` en `FailedOrder` + migración `0006` (aditiva, nullable).
3. `_verificar_venta_duplicada()` en `services.py` — alerta a Sentry si un mismo
   pedido llega a tener dos ventas distintas.
4. Documentar el quirk en `bims-api-reference.md` y `CLAUDE.md`.

Objetivo al terminar: **69 tests** (59 actuales + 10 nuevos).

**Para retomar:** ejecutar el plan tarea por tarea. La Task 1 deja la suite en rojo
transitoriamente (services.py todavía desempaqueta 2 valores); se cierra en la Task 3.

---

## Trampas conocidas

**Autenticación git.** Si un push devuelve `403 Permission to MuCi-CV/... denied to
MuCi-CV` —el dueño rechazado en su propio repo— **no es un problema de cuenta: es que
el token tiene `Contents: Read-only`**. Se arregla en Settings → Developer settings →
Fine-grained tokens → el token → Permissions → Contents → *Read and write*. Pasó
exactamente eso durante esta sesión.

El remoto `origin` tiene además un PAT muerto (401) embebido en la URL.
Y hay una limitación de GitHub que cuesta descubrir: **un PAT fine-grained solo opera
sobre repos de su propio resource owner**, nunca sobre repos de otra cuenta personal
aunque seas colaborador. Como el repo lo posee `MuCi-CV` —que es cuenta de *usuario*,
no organización— los tokens fine-grained de `carlvallory` dan 403 pese a tener
`push: true`. Para pushear: PAT desde la cuenta `MuCi-CV`, o PAT clásico (`ghp_`) con
scope `repo` desde `carlvallory`, o arreglar SSH.

**Deploy.** La migración `0006` del trabajo pendiente exige `migrate`. Verificar antes
con `showmigrations core` en el servidor. Las órdenes ya procesadas quedan con
`bims_sale_id = NULL`, así que la detección empieza a funcionar recién en el segundo
procesamiento de cada orden posterior al deploy — que es justo el caso a vigilar.

**Archivos sin trackear.** Entre ellos `core_contactcache_202603311146.csv`, con datos
de contactos. El repo es público: no hacer `git add .` sin revisar. Conviene sumarlo
al `.gitignore`.
