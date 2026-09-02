#!/usr/bin/env bash
#
# Barrido de stock BIMS → WooCommerce, para el cron. Cada 15 minutos.
#
# `flock -n` sale sin hacer nada si ya hay una corrida en curso: un barrido
# lento se solaparía con el siguiente y los dos escribirían los mismos
# productos.
#
# El `cd` NO es cosmético. `settings.py` carga la configuración con
# `dotenv_values(".env")`, que es una ruta RELATIVA: desde el cron el directorio
# de trabajo es el home, ahí no hay `.env`, y settings revienta en
# `config.get("DEBUG").lower()` sobre None antes de llegar a Django.
#
# Línea de cron (la instala Carlos, necesita root):
#   */15 * * * * root /var/www/integrador/sync-stock.sh >> /var/log/sync-stock.log 2>&1
#
# ⚠️ Y con logrotate desde el día uno: `/var/log/process-queue.log` se instaló
# sin rotación el 2026-09-02 y ya es deuda.
#
# ⚠️ El barrido arranca en modo SECO (`STOCK_SYNC_ENABLED=false`): instalar esta
# línea no cambia nada en la web hasta que alguien prenda el flag en el `.env`.
# Es deliberado — el primer barrido se mira antes de aplicarlo.
set -euo pipefail

cd /var/www/integrador

exec /usr/bin/flock -n /var/lock/sync-stock.lock \
    /root/venv-integrador-52/bin/python manage.py sync_stock
