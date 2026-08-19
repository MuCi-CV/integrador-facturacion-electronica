# Handoff — sesión del 2026-08-19

> Sesión corta y de dos mitades: se cerró la limpieza pendiente del merge anterior y se arrancó
> el diseño del próximo objetivo grande. El diseño **se frenó a propósito** en su primera
> pregunta, porque depende de un dato que no teníamos a mano.

## Estado al cierre

| | |
|---|---|
| Rama activa | `main` — working tree limpio |
| Remoto | `github/main` sincronizado (el handoff del 18 se pusheó: `370e99a..bd5a3d2`) |
| Ramas remotas | `feature/omitir-productos-monto-cero` **borrada** |
| Producción | Sigue corriendo `08c7f7c` **en la rama vieja** — el pase a `main` NO se hizo |
| Código | Sin cambios: cero commits de código hoy |

---

## 1. Lo que se cerró hoy

### Push del handoff y borrado de la rama mergeada

Ambos hechos. Lo relevante no es el resultado sino **cómo**: se pusheó con el token `gho_` de
`gh` a través de un credential helper efímero, sin PAT y sin persistir nada.

```bash
git -c credential.helper='!f() { echo username=x-access-token; echo "password=$(gh auth token)"; }; f' push github main
```

Esto **corrige una creencia previa** de los handoffs anteriores: se daba por hecho que el token de
`gh` solo servía para la API (abrir PRs) y que para pushear hacía falta un PAT de la cuenta
`MuCi-CV`. No es así — los tokens OAuth de `gh` se comportan como los clásicos. De acá en más,
pushear no requiere ningún PAT.

### Orden alterado en el borrado de la rama

El handoff del 18 pedía borrar la rama remota **después** de pasar producción a `main`. Se borró
antes. Se verificó primero que su tip (`370e99a`) fuera ancestro de `main`, así que **ningún commit
se perdió** — `main` los contiene a todos.

Consecuencia real, una sola: el servidor sigue con esa rama en checkout, y ahora un `git pull`
pelado ahí falla con *"your configuration specifies to merge with the ref ... which does not
exist"*. El deploy de abajo no se ve afectado porque arranca con `fetch --prune` + `checkout main`.
Si por algún motivo hiciera falta revivirla:

```bash
git push github 370e99a:refs/heads/feature/omitir-productos-monto-cero
```

### Lo que NO se hizo, y por qué

| Pendiente | Motivo |
|---|---|
| Pasar producción a `main` | **Sin acceso.** La clave `anthropic_readonly_muciserver` no es aceptada por `root@integrador.muci.org` (159.89.228.18): `Permission denied (publickey)`. Lo tiene que correr Carlos. |
| Revocar el PAT | Decisión explícita de Carlos: no esta semana. |
| Chequeos de `runretryfaileds.sh` | Postergado hasta terminar el objetivo en curso. |

**Deploy pendiente** (el `cp` no es opcional: `main` no trackea `bims_api.log`, así que el checkout
lo borra del working tree, y sin reiniciar el proceso sigue escribiendo a un inodo borrado):

```bash
cd /var/www/integrador
cp bims_api.log /root/bims_api.log.bak-$(date +%F)
git fetch origin --prune
git checkout main && git pull origin main
/root/.local/share/virtualenvs/integrador-ObaHlHmv/bin/python manage.py migrate
systemctl restart mucintegrador.service
systemctl is-active mucintegrador.service
```

---

## 2. El próximo objetivo: preparar el integrador para el hub → CRM

Se abrió el diseño de lo que pide `/home/vallory/IA/arquitectura/integrador-facturacion-flujo.md`
(el doc vive **fuera del repo**, no se carga solo). Clasificado como trabajo **arquitectural**:
brainstorming → spec → plan. **No hay rama, ni spec, ni código todavía.**

### Descomposición en cinco sub-proyectos

El objetivo es demasiado grande para una sola spec. Cada uno lleva su propio ciclo:

| | Sub-proyecto | Depende de |
|---|---|---|
| **A** | Registro del evento al ingresar + estado por rama | — |
| **B** | Modelo interno de pedido + cliente de origen tras una interfaz | — |
| **C** | Rama CRM Krayin (pasos 1 y 4 del doc) | A |
| **D** | Reintentos con backoff por rama | A |
| **E** | Adaptador PrestaShop / Ticketera 2.0 | B |

**Carlos eligió empezar por A**, porque es el único que arregla algo que ya está roto hoy y porque
C y D se apoyan encima.

### Los cuatro hallazgos que justifican A

Verificados contra el código, no contra el doc:

1. **`FailedOrder` no puede expresar el estado que el doc pide.** Un solo eje (`status`:
   FAILED/COMPLETED, `models.py:29`) para toda la orden. No hay dónde escribir "BIMS ok, CRM
   pendiente" — que es la primera fila de la tabla de errores del doc de arquitectura.
2. **Nada se persiste al ingresar.** La primera escritura ocurre *después* de consultar WooCommerce
   (`services.py:473`). Un crash antes de eso pierde el pedido sin dejar rastro.
3. **El campo libre `message` ya funciona de canal de estado improvisado.**
   `sync_bims_contacts.py:63` filtra por `message__startswith="Pausada: Esperando"`. Reformular ese
   texto rompe el comando en silencio — el mismo tipo de trampa que ya vigilamos con
   `ZERO_PRICE_SKIP_REASON`.
4. **`order_id` no tiene constraint `unique`** (`models.py:28`). Dos webhooks simultáneos de la misma
   orden pueden crear dos filas; desde ahí, todo `update_or_create` de esa orden explota con
   `MultipleObjectsReturned`.

Consumidores de `FailedOrder` que A va a tener que migrar: `services.py`, `admin.py` (dos vistas
custom + una acción), `retryfaileds.py`, `sync_bims_contacts.py` y `tests.py`.

---

## 3. La pregunta que frenó la sesión

**¿El procesamiento sigue dentro del request HTTP, o pasa a 202 + worker diferido?**

Carlos se inclina por async, pero le preocupa que **la cantidad de workers sea un limitante para la
capacidad del servidor**. Decidió no resolverlo a ojo. Es la decisión correcta: no se resuelve
opinando.

Persistir el evento antes de procesar funciona en los dos esquemas; lo que cambia es **quién
procesa**. Hoy `SalesView.post()` espera a que `process_order` termine —Woo, BIMS, caché de
contactos— antes de responder.

### Datos que hay que juntar para responderla

En el servidor:

```bash
nproc; free -m
systemctl cat mucintegrador.service | grep -i exec    # ¿cuántos workers hay hoy?
```

Y una pregunta que no es técnica: **¿quién llama a `/sales/` y qué hace con la respuesta?** Si es un
webhook que ignora el body, pasar a 202 es gratis. Si del otro lado hay un plugin mostrándole el
error a un cajero en el punto de venta, cambiar el contrato le rompe la pantalla a alguien.

---

## Para retomar

1. **Correr el deploy** de la sección 1 (necesita a Carlos en el servidor).
2. **Juntar los datos de capacidad** de la sección 3. Con eso se destraba el diseño de A.
3. **Retomar el brainstorming de A** desde esa pregunta. El resto del contexto está en la memoria
   del proyecto (`project_preparacion_arquitectura_hub`).
4. Sigue vivo de antes: **vigilar Sentry** por warnings de `precio negativo`, contando desde el
   18/08 12:00 UTC. Y el **PAT sin revocar**, por decisión explícita, no por olvido.
