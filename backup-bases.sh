#!/usr/bin/env bash
#
# Dump de las cuatro bases del servidor. Primer backup que van a tener.
#
# POR QUÉ ESTE USUARIO
#   La credencial de root de MariaDB no está a mano: /root/.my.cnf no existe y
#   /etc/mysql/debian.cnf existe pero no se honra. `anthropic_readonly` tiene
#   GRANT SELECT, SHOW DATABASES ON *.*, que alcanza para un dump.
#
# DOS COSAS QUE NO SON OPCIONALES
#   --no-tablespaces: ese usuario no tiene el privilegio PROCESS y sin esto falla.
#   set -o pipefail: con el pipe a gzip, $? refleja el gzip y MIENTE. Ya reportó
#   éxito sobre un gzip vacío de 20 bytes.
#
# LA CLAVE NO VA EN ESTE ARCHIVO
#   Se pasa por entorno (MYSQL_PWD), nunca hardcodeada ni por línea de comandos:
#   `-p<clave>` queda visible en la lista de procesos para cualquier usuario.
#
# USO
#   MYSQL_PWD=<pass> ./backup-bases.sh [etiqueta]
set -euo pipefail
set -o pipefail

ETIQUETA="${1:-$(date -u +%Y%m%dT%H%M%SZ)}"
DESTINO="/root/bk/db-${ETIQUETA}.sql.gz"
BASES="muci muci-integrador krayin moodle"

if [ -z "${MYSQL_PWD:-}" ]; then
    echo "ERROR: exportá MYSQL_PWD con la clave de anthropic_readonly" >&2
    exit 2
fi

mkdir -p /root/bk
echo "==> Volcando: $BASES"
mariadb-dump -u anthropic_readonly -B $BASES \
    --single-transaction --quick --no-tablespaces \
  | gzip > "$DESTINO"

echo "==> Verificando que el dump esté completo"
ULTIMA="$(zcat "$DESTINO" | tail -1)"
if ! echo "$ULTIMA" | grep -q "Dump completed on"; then
    echo "ERROR: el dump NO terminó bien. Última línea: $ULTIMA" >&2
    exit 1
fi

echo "==> OK  $DESTINO  ($(du -h "$DESTINO" | cut -f1))"
echo "    última línea: $ULTIMA"
for b in $BASES; do
    n="$(zcat "$DESTINO" | grep -c "^-- Current Database: \`$b\`" || true)"
    echo "    base '$b' presente en el dump: $n"
done
