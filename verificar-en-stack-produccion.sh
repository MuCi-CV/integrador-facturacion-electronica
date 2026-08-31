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
# SALVEDAD DEL MODO POR DEFECTO
#   El Python 3.7 del sistema tiene Django 3.2.18, no el 3.2.25 exacto del venv de
#   producción (7 releases de parche dentro de la misma minor). Prueba
#   compatibilidad con 3.7 y con la API de Django 3.2; no prueba el venv exacto.
#   Para eso está `PYTHON=`, abajo, que sí necesita root.
#
# USO
#   ./verificar-en-stack-produccion.sh            # verifica HEAD
#   ./verificar-en-stack-produccion.sh <rama>     # verifica otra ref
#
#   Sobre el venv EXACTO de producción (Django 3.2.25), con root:
#     PYTHON=/root/.local/share/virtualenvs/integrador-ObaHlHmv/bin/python \
#       SERVIDOR=root@muci.org REMOTO=wt-verificacion-venv \
#       ./verificar-en-stack-produccion.sh
#   Corrido así el 2026-08-27 sobre 11d4780: 152/152 en 3.7.17 + Django 3.2.25.
#
# OJO CON LA LLAVE
#   Los `ssh` fuerzan `IdentitiesOnly=yes`. Sin eso, una máquina con varias llaves
#   en el agente las ofrece todas y el server corta con "Too many authentication
#   failures" antes de llegar a la de $LLAVE.
#
# Devuelve el código de salida de la suite, así que sirve en un pipeline.

set -euo pipefail

LLAVE="${LLAVE:-$HOME/.ssh/muci}"
SERVIDOR="${SERVIDOR:-anthropic_readonly@muci.org}"
REMOTO="${REMOTO:-wt-verificacion}"
REF="${1:-HEAD}"
# Intérprete remoto. Por defecto el 3.7 del sistema (Django 3.2.18). Para probar el venv
# exacto de producción (Django 3.2.25) exportá:
#   PYTHON=/root/.local/share/virtualenvs/integrador-ObaHlHmv/bin/python SERVIDOR=root@muci.org
PYTHON="${PYTHON:-/usr/bin/python3.7}"

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
    | ssh -i "$LLAVE" -o IdentitiesOnly=yes -o ConnectTimeout=20 "$SERVIDOR" \
        "rm -rf ~/'$REMOTO' && mkdir -p ~/'$REMOTO' && tar -x -C ~/'$REMOTO'"

limpiar() {
    echo "==> Borrando ~/$REMOTO del servidor"
    ssh -i "$LLAVE" -o IdentitiesOnly=yes -o ConnectTimeout=20 "$SERVIDOR" "rm -rf ~/'$REMOTO'" || true
}
trap limpiar EXIT

echo "==> Stack y compilación"
ssh -i "$LLAVE" -o IdentitiesOnly=yes -o ConnectTimeout=30 "$SERVIDOR" "
    cd ~/'$REMOTO'
    $PYTHON -c 'import sys, django; print(\"Python\", sys.version.split()[0], \"| Django\", django.get_version())'
    find core muci-integrador -name '*.py' -print0 | xargs -0 $PYTHON -m py_compile
    echo 'Todos los .py compilan'
"

echo "==> Suite completa"
set +e
ssh -i "$LLAVE" -o IdentitiesOnly=yes -o ConnectTimeout=120 "$SERVIDOR" \
    "cd ~/'$REMOTO' && $PYTHON manage.py test core/ --settings=muci-integrador.test_settings"
CODIGO=$?
set -e

if [ "$CODIGO" -eq 0 ]; then
    echo "==> VERDE sobre el stack de producción ($COMMIT)"
else
    echo "==> ROJO sobre el stack de producción ($COMMIT) — código $CODIGO" >&2
fi

exit "$CODIGO"
