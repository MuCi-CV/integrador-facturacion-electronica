#!/usr/bin/env bash
#
# Worker de la cola, para el cron. `flock -n` sale sin hacer nada si ya hay una
# corrida en curso: sin eso, una corrida lenta se solaparía con la siguiente y
# dos workers llamarían a BIMS por la misma orden.
#
# El `cd` NO es cosmético. `settings.py` carga la configuración con
# `dotenv_values(".env")`, que es una ruta RELATIVA: desde el cron el directorio
# de trabajo es el home, ahí no hay `.env`, y settings revienta en
# `config.get("DEBUG").lower()` sobre None antes de llegar a Django.
#
# Rutas verificadas contra el servidor el 2026-09-01: `/var/www/integrador` es el
# checkout real y `/root/venv-integrador-52/bin/python` el intérprete que corre
# gunicorn hoy. OJO: `runretryfaileds.sh` todavía apunta a
# `/var/www/integrador.muci.org/backend`, que NO existe.
#
# Línea de cron (la instala Carlos en el Despliegue 2):
#   * * * * * /var/www/integrador/process-queue.sh >> /var/log/process-queue.log 2>&1
set -euo pipefail

cd /var/www/integrador

exec /usr/bin/flock -n /var/lock/process-queue.lock \
    /root/venv-integrador-52/bin/python manage.py process_queue
