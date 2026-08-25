# Spec — Saneamiento del `python3` del sistema en producción

**Fecha:** 2026-08-25
**Rama:** `fix/saneamiento-python3-sistema`
**Estado:** Aprobado para ejecución. Pre-vuelo verificado en verde el 2026-08-25.
**Ejecuta:** Carlos, manualmente, en una ventana elegida (prevista 13:00 hora de Asunción).
**Relación con otros proyectos:** es **prerrequisito** de la migración del integrador a
Python 3.12 + Django 5.2 (proyecto B, sin spec todavía).

## Problema

En el servidor de producción, `/usr/bin/python3` está bajo `update-alternatives` en
**modo manual**, apuntando a `/usr/bin/python3.7`:

```
python3 - manual mode
  link best version is /usr/bin/python3.10
  link currently points to /usr/bin/python3.7
/usr/bin/python3.10 - priority 2
/usr/bin/python3.7 - priority 1
```

Esto no es la configuración de la distro. En Debian/Ubuntu `/usr/bin/python3` es un
symlink de `python3-minimal` y **no debe** gestionarse por alternatives, porque decenas
de herramientas del sistema traen `#!/usr/bin/python3` en el shebang y esperan el Python
para el que están compilados los `dist-packages` (en Ubuntu 22.04, **3.10**).

Alguien forzó 3.7 —casi seguro para poder correr `python3 manage.py` con el stack que
instaló en `/usr/local/lib/python3.7/dist-packages/`— y eso rompió en silencio todo el
tooling Python de Ubuntu.

### El problema de seguridad real

No es que Python 3.7 esté en EOL (junio 2023) y sin parches. **Es que el servidor dejó
de poder aplicar parches.**

| Componente | Estado hoy | Causa |
|---|---|---|
| `unattended-upgrades` | `enabled` pero **`failed`**, sin logs en `/var/log/unattended-upgrades/` | shebang `#!/usr/bin/python3` → 3.7 → no importa `apt_pkg` |
| `apt-check` | revienta | `import apt` → `apt_pkg` |
| `pro` / `ubuntu-advantage` | `ModuleNotFoundError: No module named 'apt_pkg'` | ídem |
| `add-apt-repository` | roto | ídem |
| `do-release-upgrade` | roto | ídem |

Verificación directa:

```
python3   -c "import apt_pkg"  →  ModuleNotFoundError
python3   -c "import gi"       →  ImportError: cannot import name '_gi'
python3.10 -c "import apt_pkg" →  OK
python3.10 -c "import gi"      →  OK
```

**Las actualizaciones automáticas de seguridad no están corriendo.**

## Objetivo

Devolver `/usr/bin/python3` a **3.10** (la versión nativa de Ubuntu 22.04, ya instalada),
restaurando el tooling del sistema, **sin que ningún servicio en ejecución deje de
funcionar**.

## No objetivos

Explícitamente fuera de alcance, cada uno es su propia ventana o su propio proyecto:

- **Aplicar las actualizaciones de seguridad pendientes.** `apt-check` probablemente
  destape un backlog grande acumulado desde que esto se rompió. Decisión y ventana aparte.
- **Migrar el integrador a Python 3.12 + Django 5.2** (proyecto B).
- **Subir Ubuntu 22.04 → 24.04.**
- **Limpiar el stack huérfano** de `/usr/local/lib/python3.7/dist-packages/`.
- **Migrar supervisord u otros servicios** a un Python más nuevo.

## Análisis de aislamiento

Es el corazón de la spec: la pregunta que había que responder era *¿qué deja de andar?*
La respuesta, verificada componente por componente, es **nada**.

### Inmunes — no pasan por `/usr/bin/python3`

| Componente | Evidencia |
|---|---|
| **Integrador (gunicorn)** | `ExecStart` usa ruta absoluta al venv. Y el único symlink que sale del venv es `bin/python → /usr/bin/python3.7m`, **absoluto y versionado**. `pyvenv.cfg` lo confirma: `base-executable = /usr/bin/python3.7m`. Los demás (`python3`, `python3.7`, `python3.7m`) son relativos e internos. |
| **supervisord** + worker de bot-whatsapp | Paquete de distro en `/usr/lib/python3/dist-packages/supervisor/`, Python puro. `import supervisor` funciona bajo 3.7 **y** 3.10. Además solo administra un programa, y es PHP (`php8.4 artisan queue:work`). |
| **pipenv** | Paquete de distro en `/usr/lib/python3/dist-packages/pipenv/`. Importa bajo ambas. Corre bajo 3.10 tras el cambio, pero ejecuta el `bin/python` del venv, así que Django sigue en 3.7. |
| **certbot** | Es snap (`/usr/bin/certbot → /snap/bin/certbot`, 5.7.0 classic) con Python embebido. La renovación de TLS es ajena a las alternatives del sistema. |
| **Crons de PHP y utilidades** | `wp-cron.php` (php8.4), `artisan schedule:run` (php8.2), `find ... -delete`, `systemctl restart`. Sin Python. |

### Rotos hoy — el cambio los arregla

`unattended-upgrades`, `apt-check`, `pro`/`ubuntu-advantage` (`ua-timer.service`,
`ubuntu-advantage.service`, `ua-reboot-cmds.service`), `add-apt-repository`,
`do-release-upgrade`. Todos fallan hoy; ninguno puede empeorar.

### Huérfano — sin consumidores

El stack de Django 3.2.18 en `/usr/local/lib/python3.7/dist-packages/` (Django, gunicorn,
drf_yasg, DRF, PyMySQL, WooCommerce, cryptography). Se verificó que **nada lo invoca**:

- Crontabs de **todos** los usuarios del sistema: una sola línea con Python, y usa
  `pipenv run` (`0 0 * * * cd /var/www/integrador && /usr/bin/pipenv run python manage.py sync_bims_contacts`).
- `/etc/cron.d/`: solo `e2scrub_all` y `php`.
- `/etc/cron.daily|hourly|weekly`: sin Python.
- Unidades systemd: la única con Python propio es `mucintegrador.service` (venv); las
  `ua-*` usan `/usr/bin/python3` y ya están rotas.

Queda en disco sin molestar. Limpiarlo es otro trabajo.

## Regla permanente

> **`/usr/bin/python3` debe quedarse en 3.10 en Ubuntu 22.04, incluso después de que el
> proyecto B instale Python 3.12.**

Los `dist-packages` del sistema están compilados para 3.10. Mover `python3` a 3.12
reproduciría exactamente este mismo desastre. El proyecto B instalará 3.12 como binario
**adicional** (`/usr/bin/python3.12`) y construirá el venv del integrador con
`pipenv --python /usr/bin/python3.12`, más `[requires] python_version` en el `Pipfile`.
El integrador nunca vuelve a depender de a qué apunte `python3`.

Por eso se usa `--set` (modo manual explícito) y no `--auto`: con `--auto`, instalar 3.12
con prioridad más alta movería `python3` solo.

## Runbook

### 1. Pre-vuelo — YA EJECUTADO, en verde (2026-08-25)

```
for u in $(cut -d: -f1 /etc/passwd); do crontab -l -u $u 2>/dev/null | grep -Hn python; done; systemctl is-active mucintegrador
```

Resultado: una sola coincidencia (la línea de `pipenv run` de root) y `active`.

Repetir en la ventana. Tener una **segunda sesión SSH abierta** como red de seguridad.

### 2. El cambio

```
update-alternatives --set python3 /usr/bin/python3.10 && python3 --version
```

Esperado: `Python 3.10.x`.

### 3. Momento de la verdad — el integrador

```
systemctl restart mucintegrador; systemctl is-active mucintegrador; /root/.local/share/virtualenvs/integrador-ObaHlHmv/bin/python --version
```

Esperado: `active` y `Python 3.7.17`. **Si no da `active`, ir directo a Vuelta atrás.**

### 4. El único cron con Python

```
cd /var/www/integrador && /usr/bin/pipenv run python -V && /usr/bin/pipenv run python manage.py check
```

Esperado: `3.7.17` y `System check identified no issues`.

Validación fuerte opcional: correr `sync_bims_contacts` completo. Son 38 requests
paginados de **solo lectura** contra BIMS, tarda un par de minutos y es idempotente.

### 5. Confirmar lo que se quería arreglar

```
python3 -c "import apt_pkg; print('ok')"; /usr/lib/update-notifier/apt-check --human-readable; pro --version; systemctl start unattended-upgrades; systemctl is-failed unattended-upgrades
```

### 6. Confirmar que nada más se cayó

```
systemctl is-active supervisor; supervisorctl status
```

### 7. Segunda validación, gratis

El reinicio automático del cron (`0 */6`, o sea 00/06/12/18 UTC = 21/03/09/15 hora de
Asunción). Si el servicio sobrevive el reinicio manual del paso 3 **y** el automático
siguiente, quedó probado dos veces.

## Vuelta atrás

Un comando, sin depurar en caliente:

```
update-alternatives --set python3 /usr/bin/python3.7; systemctl restart mucintegrador
```

**Disparador:** el paso 3 no devuelve `active`. Se revierte primero y se investiga
después. La exposición son segundos, en una ventana elegida.

No hay estado persistente que revertir: el cambio es un symlink, no toca base de datos ni
paquetes.

## Criterios de éxito

1. `python3 --version` → 3.10.x
2. `mucintegrador` sigue `active`, con el venv en 3.7.17
3. `pipenv run python manage.py check` limpio
4. `import apt_pkg` funciona bajo `python3`
5. `unattended-upgrades` sale de `failed`
6. `supervisorctl status` sin cambios
7. El servicio sobrevive el siguiente reinicio automático

## Riesgos residuales

- **pipenv bajo 3.10.** Importa bien y localiza el venv por hash del directorio del
  proyecto, no por intérprete. Como el `Pipfile` no declara `[requires] python_version`,
  no hay chequeo de coincidencia que pueda fallar. El paso 4 lo verifica de todos modos.
- **Backlog de actualizaciones.** Al arreglarse, `unattended-upgrades` podría empezar a
  aplicar actualizaciones acumuladas por su cuenta en su próxima corrida. Si eso no se
  desea en la misma ventana, considerar detener el timer hasta decidir la ventana de
  parches.
- **Ventana horaria.** 13:00 de Asunción queda a dos horas del reinicio automático
  anterior (09:00) y dos del siguiente (15:00). Margen cómodo por ambos lados.
