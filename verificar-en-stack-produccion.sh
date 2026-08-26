#!/usr/bin/env bash
#
# Corre la suite de tests sobre el stack REAL de producción (Python 3.7 + Django 3.2)
# antes de aprobar un despliegue.
#
# POR QUÉ EXISTE
#   Local corre Python 3.12 + Django 6.0.3; producción corre Python 3.7.17 + Django
#   3.2.25. Un "todo en verde" local NO prueba compatibilidad con producción. Esa
#   suposición ya falló una vez (2026-08-25).
#
# CORRE EN EL SERVIDOR, PERO NO TOCA PRODUCCIÓN
#   Usa el Python 3.7 del sistema, no el venv, y `muci-integrador/test_settings.py`
#   es autónomo: SQLite en memoria, hosts de BIMS/WooCommerce inventados, el `.env`
#   nunca se lee. Los tests además interceptan `HTTPAdapter.send`, así que no sale
#   un paquete de la máquina. El checkout de `/var/www/integrador` no se toca y el
#   servicio no se entera. El directorio temporal se borra siempre, aunque los
#   tests fallen.
#
# SALVEDAD
#   El Python 3.7 del sistema tiene Django 3.2.18, no el 3.2.25 exacto del venv de
#   producción (7 releases de parche dentro de la misma minor). Prueba
#   compatibilidad con 3.7 y con la API de Django 3.2; no prueba el venv exacto.
#   Esa corrida necesita root.
#
# USO
#   ./verificar-en-stack-produccion.sh            # verifica HEAD
#   ./verificar-en-stack-produccion.sh <rama>     # verifica otra ref
#
# Devuelve el código de salida de la suite, así que sirve en un pipeline.

set -euo pipefail

LLAVE="${LLAVE:-$HOME/.ssh/muci}"
SERVIDOR="${SERVIDOR:-anthropic_readonly@muci.org}"
REMOTO="${REMOTO:-wt-verificacion}"
REF="${1:-HEAD}"

if [ ! -f "$LLAVE" ]; then
    echo "ERROR: no encuentro la llave ssh en $LLAVE" >&2
    echo "       exportá LLAVE=/ruta/a/la/llave si está en otro lado" >&2
    exit 2
fi

COMMIT="$(git rev-parse --short "$REF")"
echo "==> Enviando $REF ($COMMIT) a $SERVIDOR:~/$REMOTO"

# `git archive` manda solo archivos trackeados del commit: nada de .venv,
# __pycache__, logs ni el .env local.
git archive --format=tar "$REF" \
    | ssh -i "$LLAVE" -o ConnectTimeout=20 "$SERVIDOR" \
        "rm -rf ~/'$REMOTO' && mkdir -p ~/'$REMOTO' && tar -x -C ~/'$REMOTO'"

limpiar() {
    echo "==> Borrando ~/$REMOTO del servidor"
    ssh -i "$LLAVE" -o ConnectTimeout=20 "$SERVIDOR" "rm -rf ~/'$REMOTO'" || true
}
trap limpiar EXIT

echo "==> Stack y compilación"
ssh -i "$LLAVE" -o ConnectTimeout=30 "$SERVIDOR" "
    cd ~/'$REMOTO'
    /usr/bin/python3.7 -c 'import sys, django; print(\"Python\", sys.version.split()[0], \"| Django\", django.get_version())'
    find core muci-integrador -name '*.py' -print0 | xargs -0 /usr/bin/python3.7 -m py_compile
    echo 'Todos los .py compilan en 3.7'
"

echo "==> Suite completa"
set +e
ssh -i "$LLAVE" -o ConnectTimeout=120 "$SERVIDOR" \
    "cd ~/'$REMOTO' && /usr/bin/python3.7 manage.py test core/ --settings=muci-integrador.test_settings"
CODIGO=$?
set -e

if [ "$CODIGO" -eq 0 ]; then
    echo "==> VERDE sobre el stack de producción ($COMMIT)"
else
    echo "==> ROJO sobre el stack de producción ($COMMIT) — código $CODIGO" >&2
fi

exit "$CODIGO"
