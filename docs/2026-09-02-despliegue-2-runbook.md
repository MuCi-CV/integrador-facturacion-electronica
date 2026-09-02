# Despliegue 2 — el cambio de contrato del ingreso

**Rama:** `feature/hub-ingreso-cola` @ `07b9eb7` (pusheada a `github`)
**Merge a `main`:** fast-forward limpio, verificado (`main` es ancestro, 0 commits de diferencia)
**Producción hoy:** `main` @ `7704044`, migraciones de `core` hasta la **`0011`**

Este despliegue cambia el contrato de `POST /sales/`: pasa de facturar en línea y devolver 200 a
persistir y devolver **202**. Desde ese momento **lo único que factura es el worker de la cola**.

---

## ⚠️ Tres correcciones al plan original (Tarea 9)

1. 🔴 **El plan dice "sin `migrate` (no hay migraciones nuevas)". Es FALSO.** Hay **dos**
   migraciones pendientes: la **`0012`** (limpieza del canal `PAUSADA`, de la Tarea 4) y la
   **`0013`** (`AlertThrottle`, de la Tarea 8). Verificado contra `django_migrations`: producción
   está en la `0011`. **Sin `migrate`, el worker revienta** al primer aviso porque
   `core_alertthrottle` no existe.
2. **El rollback vuelve a `7704044`, no a `43fd813`.** `43fd813` era el rollback del Despliegue 1.
3. **El orden importa:** código y `migrate` **antes** del cron. Si el cron se instala primero,
   `process-queue.sh` llama a un comando que todavía no existe y falla cada minuto.

## ⚠️ La ventana sin facturación, y cómo hacerla corta

Entre el `restart` (el ingreso ya devuelve 202 y encola) y el cron instalado (lo único que vacía la
cola) **no se factura nada**. Las ventas no se pierden —quedan `PENDING`— pero no se emiten.

Por eso el paso 6 corre el worker **a mano una vez** antes de instalar el cron: si algo está mal,
se ve ahí, con la cola de un solo dígito, y no con el cron martillando cada minuto.

---

## Estado medido justo antes (2026-09-02)

| | |
|---|---|
| filas totales | 8729 (201 `FAILED`, 8528 `COMPLETED`) |
| filas `PENDING` / `PROCESSING` | **0** (el código de la cola no está desplegado) |
| `0012` sigue siendo no-op | ✅ **0 filas** `"Pausada: Esperando"` |
| `core_alertthrottle` | no existe todavía |
| Tests | **241 OK** en local (3.12 + Django 5.2.17) y **VERDE** sobre el stack de rollback (3.7 + Django 3.2), en `07b9eb7` |

---

## Pasos

### 1. Verificación sobre el stack REAL — la corre Carlos (necesita root)

```bash
PYTHON=/root/venv-integrador-52/bin/python SERVIDOR=root@muci.org REMOTO=wt-verificacion-52 \
  ./verificar-en-stack-produccion.sh
```

**Esperado: 241 OK.** Es la única corrida que prueba el stack que sirve tráfico hoy (3.10.12 +
Django 5.2.17); la mía prueba el de rollback.

### 2. Dump nuevo — Carlos

El que hay (`/root/bk/db-pre-expansion.sql.gz`, 226 MB) es del 01/09 y ya tiene dos días de ventas
de diferencia. Este despliegue trae DDL (`0013`), así que conviene uno fresco.

### 3. `SLACK_WEBHOOK_URL` en el `.env` — Carlos, ANTES del deploy

Sin webhook las alertas se callan y **el 202 esconde todo**: el despliegue entra sin la red de
seguridad que lo vuelve seguro. Las otras dos variables (`QUEUE_ALERT_THRESHOLD`,
`QUEUE_SILENCE_MINUTES`) tienen default 10 y pueden omitirse.

⚠️ `settings.py` lee el `.env` con `dotenv_values()` **en el import**: editarlo no surte efecto
hasta el `restart` del paso 5.

### 4. Merge y push de `main`

```bash
git checkout main && git merge --ff-only feature/hub-ingreso-cola && git push github main
```

`--ff-only` a propósito: si no es fast-forward, algo cambió desde esta verificación y hay que
mirarlo antes de seguir.

### 5. Código + migraciones + restart — Carlos (root)

```bash
ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org '
  cd /var/www/integrador &&
  git pull &&
  /root/venv-integrador-52/bin/python manage.py migrate &&
  systemctl restart mucintegrador.service &&
  systemctl show mucintegrador.service -p ActiveEnterTimestamp -p ActiveState'
```

**Esperado:** `migrate` aplica `0012` y `0013`; el servicio queda `active`.

### 6. Correr el worker UNA VEZ a mano, antes del cron — Carlos (root)

```bash
ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org \
  'cd /var/www/integrador && /root/venv-integrador-52/bin/python manage.py process_queue'
```

Debe terminar sin excepción. Si no hay nada encolado no imprime casi nada, y eso está bien.

### 7. Instalar el cron — Carlos (root)

```bash
ssh -i ~/.ssh/muci -o IdentitiesOnly=yes root@muci.org '
  grep -q process-queue /etc/crontab ||
  echo "* * * * * root /var/www/integrador/process-queue.sh >> /var/log/process-queue.log 2>&1" >> /etc/crontab
  grep -c process-queue /etc/crontab'
```

**Esperado: `1`.** El `grep -q ||` lo hace idempotente. `process-queue.sh` ya viene con modo
`100755` en git, así que el checkout lo deja ejecutable.

### 8. Probar la alerta a propósito — **una alerta que nunca se probó no es una alerta**

Encolar una referencia que va a fallar y confirmar que el mensaje llega a Slack.

### 9. Verificar con una venta real

- `POST /sales/` responde **202** en milisegundos
- en menos de ~1 minuto la fila pasa a `COMPLETED` con `bims_sale_id`
- la orden de Woo queda con `_bims_sale_id`
- `tail -f /var/log/process-queue.log` muestra al worker corriendo cada minuto

### 10. Vigilar el resto del día

Que la cola no crezca, que no queden `PROCESSING` colgadas, y que la alerta de tamaño no dispare —
o que dispare, y **ahí tengamos por fin el dato del pico** para decidir si el worker necesita
paralelismo.

---

## Rollback

```bash
# 1. sacar el cron primero, o seguirá corriendo contra el código viejo
ssh root@muci.org "sed -i '/process-queue/d' /etc/crontab"
# 2. código al Despliegue 1
ssh root@muci.org 'cd /var/www/integrador && git reset --hard 7704044 && systemctl restart mucintegrador.service'
```

**Las migraciones `0012` y `0013` se quedan aplicadas.** La `0012` es un no-op (0 filas) y la `0013`
crea una tabla que el código viejo ignora.

### 🔴 La trampa del rollback, que hay que saber ANTES de necesitarlo

Al volver al código viejo, **las filas que quedaron en `PENDING` (3) o `PROCESSING` (4) quedan
huérfanas**: el código viejo no conoce esos estados, no tiene worker que las procese, y
`retryfaileds` filtra por `status=FAILED`, así que **no las toca**. Son ventas encoladas que nadie
va a facturar.

Antes de dar el rollback por terminado, contarlas y decidir:

```sql
SELECT status, COUNT(*) FROM core_failedorder WHERE status IN (3,4) GROUP BY status;
```

Si hay, la salida más simple es pasarlas a `FAILED` para que el camino viejo las vea:

```sql
UPDATE core_failedorder SET status = 1 WHERE status IN (3,4);
```

Y después reintentarlas por el admin. **Esto es una escritura en producción: la corre Carlos.**

---

## Lo que este despliegue cierra

`SalesView` devolvía **503 ante cualquier excepción**, y Woo **deshabilita el webhook a las 5 fallas
seguidas**. Una caída de BIMS de cinco órdenes apagaba `Venta Entrada` y **la facturación se cortaba
en silencio**. Ya le pasó a `Refund order` (`failure_count 6`).

No es teórico: el 2026-09-02 se encontró que las órdenes **192578 y 192584** llegaron al integrador
el 14 y el 16/07 —alguien las reenviaba a mano cambiando el estado—, resolvieron el contacto y
**murieron sin dejar ni error ni fila**. Con la cola, esas dos habrían quedado `PENDING` y el worker
las habría reintentado con backoff.
