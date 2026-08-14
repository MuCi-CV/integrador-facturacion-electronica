# Handoff — sesión del 2026-08-13

## Estado al cierre

| | |
|---|---|
| Rama activa | `feature/omitir-productos-monto-cero` — pusheada (`08c7f7c`), 4 commits sobre `main` |
| `main` | sin tocar (`e53b849`) |
| Suite de tests | 79/79 en verde (59 previos + 20 nuevos) |
| Migraciones | ninguna — no se tocaron modelos |
| Trabajo pendiente | probar en producción y decidir el merge |

PR listo para abrir en:
`https://github.com/MuCi-CV/integrador-facturacion-electronica/pull/new/feature/omitir-productos-monto-cero`

---

## 1. El pedido

Que un producto con precio 0 no llegue a BIMS, y que si *todos* los productos de una
orden están en 0, la orden entera no se envíe.

El pedido original se había interpretado mal en su momento: se implementó como *SKU con
valor 0* (`core/services.py:378`, sigue vigente y es correcto), cuando lo que se quería
era **monto 0**. Esta rama implementa lo segundo; lo primero queda como estaba.

## 2. Qué llega a BIMS ahora

| Caso | Antes | Ahora |
|---|---|---|
| Producto con precio > 0 | llega | llega |
| Producto en 0 | llegaba con `price: 0.0` | omitido, sin alerta |
| Producto negativo | llegaba con precio negativo | omitido, **con alerta a Sentry** |
| Propina en 0 | llegaba como producto 100 en 0 | omitida, sin alerta |
| Propina negativa | llegaba | omitida, con alerta |
| Todos los productos en 0 | se facturaba una venta en 0 | orden descartada limpio |
| Nada válido y algún negativo | se facturaba mal | `FailedOrder` + Sentry + `ValueError` |
| Orden con total 0 | ya se bloqueaba (`services.py:459`) | igual, sin cambios |

## 3. Decisiones de diseño

**El precio 0 y el negativo se tratan distinto, a propósito.** El 0 es esperado (un
producto gratis): descarte silencioso, y si la orden queda sin productos retorna
`{"status": "Productos en 0"}` sin `FailedOrder`, sin Sentry y sin excepción. El
negativo suele ser una línea de descuento mal armada: se omite igual, pero alerta en
Sentry, y si la orden queda vacía va a `FailedOrder` como fallo.

El motivo es concreto: **descartar un negativo en silencio haría que BIMS facture más
de lo que pagó el cliente**. Es el riesgo inverso —y peor— al que se venía arreglando.

**El filtro se aplica al dict ya construido**, no a `total_with_tax`. Las tres ramas de
precio calculan distinto y la flat usa `item["total"]` sin impuesto: filtrar antes
dejaba escapar un producto flat con total 0 e impuesto > 0.

**Con propina, la propina llega sola.** Si todos los productos quedan en 0 pero hay
Tip > 0, se factura solo la propina. Decisión explícita.

**Los negativos solo se filtran en `== 0` exacto hacia abajo** (`< 0`); no hay
validación contra el precio de catálogo de BIMS.

## 4. Dos efectos colaterales, ya arreglados (`7c2159c`)

**Ruido en Sentry.** El camino de éxito alertaba cada vez que había ítems omitidos.
Con el filtro nuevo, toda orden que mezclara un producto gratis con uno pago iba a
disparar un warning aunque la venta llegara perfecta. Ahora solo alerta si algún
omitido responde a un problema de datos (sin SKU, SKU 0, cantidad 0, precio negativo).

**Órdenes atascadas en el cron.** `retryfaileds` solo cerraba la orden con
`status == "ok"`, así que una `FailedOrder` con todos sus productos en 0 se reintentaba
en cada corrida para siempre. Se agregó `DISCARDED_STATUSES` y se marcan `COMPLETED`
con el motivo (`"Cerrada sin enviar a BIMS: <motivo>."`). Se reusó `COMPLETED` en vez
de crear un estado nuevo para no pedir migración. **Esto arregló también el caso
preexistente** de `"Monto 0"`, `"Descuento 100%"` y `"No procesado"`, que ya venían
atascándose desde antes de esta rama.

## 5. Verificación hecha

- Suite completa en `main` (59, OK) contra la rama (79, OK), en worktree separado.
- **Ningún test existente fue modificado**: el único `-` del diff en `tests.py` es la
  línea de imports que se amplió.
- Sintaxis compatible con Python 3.7.17 (producción): sin walrus, `match/case`,
  `dict | dict` ni pos-only args.
- `makemigrations --check`: *No changes detected*.
- Sin cambios en `Pipfile` / `Pipfile.lock` ni variables nuevas de `.env`.
- Único consumidor de lo tocado: `core/views.py:30`. El contrato de la vista no cambia
  (el status nuevo sale como HTTP 200, igual que `"Monto 0"`).

## 6. Commits

| | |
|---|---|
| `bd7cd62` | feat(sales): omitir productos con precio 0 al armar la venta |
| `35725ed` | chore: ignorar dumps de datos, docs de apiary y artefactos locales |
| `7c2159c` | fix(sales): no alertar en Sentry por productos gratis y cerrar órdenes descartadas |
| `08c7f7c` | feat(sales): omitir productos con precio negativo y alertar |

---

## Trampas conocidas

**`core/views.sentry.py` es una bomba dormida.** Tiene una copia vieja de la lógica de
venta que llama a `bims.create_sale` **sin ninguno de estos filtros**. Hoy es
inofensiva: no está ruteada en ningún `urls.py` y no está versionada (`.gitignore:9`),
así que no existe en el servidor. Si alguien la revive, se saltea todas las garantías
de esta rama. Conviene borrarla.

**La distinción esperado/fallo se hace por substring.** `ZERO_PRICE_SKIP_REASON =
"precio 0"` y `_all_skips_are_zero_price()` deciden si una orden vacía es descarte
limpio o fallo, comparando texto de mensajes. Sigue la convención preexistente de
`"sin SKU"`, pero es frágil: reformular un mensaje de omisión cambia el comportamiento
en silencio. Lo vigila `test_omitido_por_negativo_no_cuenta_como_precio_cero`.

**Deploy: es un cambio de rama, no un `git pull`.** Producción venía en
`feature/refactor-service-layer`, donde `bims_api.log` **está trackeado**; en esta rama
no lo está. El checkout **borra el archivo del working tree**. Respaldarlo si interesa
el histórico y **reiniciar el servicio después**, o el proceso sigue escribiendo a un
inodo borrado. No hay migración que correr; el rollback es volver a la rama anterior y
reiniciar.

**Ruta de producción.** La app está en `/var/www/integrador` (confirmado por `ls`).
El `backend/` que está ahí adentro solo tiene `runsyncstock.sh` y `staticfiles`.
Pero `runretryfaileds.sh` hace `cd /var/www/integrador.muci.org/backend`, que no
coincide: o hay un symlink no verificado, o el script está desactualizado y falla.
**Verificar antes de confiar en él**, sobre todo porque esta rama depende de ese script
para cerrar las órdenes que quedaron atascadas.

**El repo es público.** Se agregaron al `.gitignore` (`35725ed`) los dumps que estaban
sueltos: `*.csv` —incluye `core_contactcache_202603311146.csv`, con datos de
contactos—, `.tokensave/`, los `.md` de la API bajados de apiary y `sentry-error-*.md`.
Revisar antes de cualquier `git add .`.

**El remoto `origin` tiene un PAT en texto plano** embebido en la URL, dentro de
`.git/config`. No sirve para pushear (usar el remoto `github`), así que está expuesto
sin dar utilidad. Limpiarlo con
`git remote set-url origin https://github.com/MuCi-CV/integrador-facturacion-electronica.git`
y revocarlo en GitHub si sigue vivo.

---

## Para retomar

1. Deploy en producción: `git fetch github` + checkout de la rama + reiniciar servicio.
2. Vigilar en Sentry los warnings nuevos de `precio negativo` — si aparecen, hay
   descuentos mal armados en WooCommerce que antes se facturaban mal en silencio.
3. En los logs, `ignorada, todos los productos tienen precio 0` marca las órdenes
   descartadas enteras.
4. Con eso decidir el merge a `main`.
5. Queda pendiente de la sesión anterior, sin tocar: el plan de detección de facturas
   duplicadas en `feature/deteccion-venta-duplicada`.
