# Handoff — sesión del 2026-09-01

**Rama:** `feature/hub-ingreso-cola` @ `55146bc` — **3 commits sin pushear, nada desplegado.**
**Tests:** 217 OK en local (3.12 + Django 5.2.17) y **VERDE también sobre el stack de rollback**
(3.7 + Django 3.2), verificado con `./verificar-en-stack-produccion.sh`.

---

## 1. La notebook se colgó tres veces, y la causa era nuestra

Antes de programar nada hubo que resolver esto, porque impedía trabajar.

**Causa raíz:** `IngresoAsincronoTest` mockeaba `process_order` **sin `return_value`**. La vista
síncrona entregaba ese `MagicMock` a `Response(data=...)` y el encoder de DRF entraba en recursión
infinita: pregunta `hasattr(obj, "tolist")`, que un `MagicMock` siempre inventa, y cada vuelta
retiene un mock nuevo. **~22 GiB de RSS en unos 4 minutos.**

Repro mínima, sin base de datos ni red: `JSONRenderer().render(MagicMock())`.

Lo engañoso es que **el síntoma no se parece a la causa**: el traceback muere en
`unittest/result.py:205` (`''.join(msgLines)`), o sea al *formatear* el error, no en el render.

**No era headroom ni Chrome.** El proxy de headroom siguió logueando normal después de los dos OOM
kills; su RSS está estable en 1,67 GiB.

Los tres cuelgues: 11:30 (el kernel **no llegó a loguear nada** — congelamiento total), 11:36:27 y
11:37:50 (esos dos sí dejaron `Out of memory: Killed process (python)` con ~22 GiB de anon-rss).

**Arreglado** en `76b8c82`. Después, al implementar la Tarea 5, la vista dejó de importar
`process_order` y la clase pasó a no mockear nada: la garantía es estructural y el riesgo
desapareció del todo.

**Mitigación de máquina:** se instaló `systemd-oomd` (255.4-1ubuntu8.17) y se agregó un drop-in
propio para el corte por swap. Quedaron activas las dos redes:

| cgroup | política | dispara cuando |
|---|---|---|
| `-.slice` | `ManagedOOMSwap=kill` (drop-in propio en `/etc/systemd/system/-.slice.d/`) | swap > 90% |
| `user@1000.service` | `ManagedOOMMemoryPressure=kill` (50%) | presión PSI ≥50% por 20 s |

⚠️ **Dos advertencias.** oomd **mata el cgroup entero**, o sea la pestaña de terminal completa con
el `claude` que corra adentro. Y está verificada la **configuración**, no el comportamiento: no se
probó en vivo, porque la única prueba honesta es desbocar un proceso de verdad.

Para trabajo pesado sigue siendo más preciso el techo explícito, que mata solo el comando y deja el
traceback:

```bash
( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings )
```

---

## 2. Tarea 5 — el ingreso devuelve 202 y persiste (`e2066a1`)

`SalesView` deja de facturar en línea: persiste con `enqueue()` y devuelve **202**. El único no-2xx
que queda es el 400 por request malformado, que depende de quien llama y no de BIMS. Con eso, una
caída de BIMS ya no puede apagar el webhook de Woo.

Los **cuatro consumidores** pasaron a escribir en la BD: `retryfaileds`, el bloque de pausadas de
`sync_bims_contacts` y los dos botones del admin — estos con el mensaje corregido a "encolada(s)".

**Tres desvíos del plan, deliberados:**

1. **`PAUSED` entra en `REQUEUEABLE`.** El plan listaba `(FAILED, NOT_APPLICABLE)`, pero entonces el
   reintento de `sync_bims_contacts` no podía reencolar nada: el plan se contradecía a sí mismo.
2. **Los cuatro llaman `enqueue(external_reference or order_id, origin)`.** Las filas creadas entre
   el `migrate` y el `restart` tienen la referencia en NULL; sin el rescate, `enqueue` reventaría
   con "referencia no numérica" y esa orden no se reintentaría nunca más.
3. **`enqueue` se apoya en `upsert_state`**, no en el `get_or_create` del plan: `order_id` sigue
   siendo `NOT NULL` en la expansión.

**Tests borrados a propósito:** `RetryFailedsCommandTest` (4) y el test de pausadas por HTTP.
Probaban que los comandos interpretaran un `200`, un camino que ya no existe. Antes de borrarlos se
verificó que la garantía de fondo —que las órdenes terminales queden en `NOT_APPLICABLE`— sigue
cubierta por los tests de la Tarea 4.

---

## 3. Tarea 6 — worker y reaper (`55146bc`)

`process_queue` toma lotes de 20 con `SKIP LOCKED`, respeta `bims_next_attempt`, y marca
`PROCESSING` **antes** de llamar a BIMS y en la misma transacción que la selección. Si se marcara
después, una muerte durante la llamada dejaría la fila `PENDING` y otro worker la tomaría en
paralelo. Hay test que lo fija espiando el estado desde adentro de `process_order`.

El reaper corre primero, con umbral `QUEUE_REAPER_MINUTES` (default 10, nuevo en `.env.example`)
para no robarle la fila a un worker vivo.

**Bug del plan corregido:** `process-queue.sh` necesita **`cd /var/www/integrador`**. `settings.py`
carga la config con `dotenv_values(".env")`, ruta **relativa**; desde el home del cron no hay `.env`
y settings revienta con `AttributeError: 'NoneType' object has no attribute 'lower'` antes de llegar
a Django. Comprobado corriendo el comando desde otro directorio. Sin ese `cd`, el cron habría
fallado en el primer minuto.

**Rutas verificadas por SSH** (como `anthropic_readonly`): el checkout real es `/var/www/integrador`
y el intérprete `/root/venv-integrador-52/bin/python` (sacado del `ps` de gunicorn).

---

## 4. Para mañana

**Siguiente: Tarea 7** (reintentos por rama / backoff), después la 8 (alerta a Slack) y la 9
(Despliegue 2).

**Antes de desplegar, tres cosas:**

1. ⚠️ **Las Tareas 5 y 6 van juntas o no van.** La 5 deja de facturar en línea y la 6 es lo único
   que vacía la cola: subir solo la 5 es dejar de facturar del todo.
2. ⚠️ **La línea de cron la instala Carlos** (necesita root):
   `* * * * * /var/www/integrador/process-queue.sh >> /var/log/process-queue.log 2>&1`
3. ⚠️ **`SKIP LOCKED` no está cubierto por los tests.** Django lo ignora sin error en SQLite, así
   que la exclusión entre workers concurrentes solo se ejerce en MariaDB.

**Pendiente de revisar, posible problema vivo:** `runretryfaileds.sh` apunta a
`/var/www/integrador.muci.org/backend`, que **no existe** en el servidor. Si esa es la ruta que usa
el cron, el reintento de fallidas lleva tiempo sin correr. No se pudo confirmar: el crontab de root
no es legible como `anthropic_readonly`.
