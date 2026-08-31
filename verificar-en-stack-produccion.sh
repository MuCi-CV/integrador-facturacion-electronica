#!/usr/bin/env bash
#
# Corre la suite de tests sobre un stack del servidor antes de aprobar un despliegue.
#
# ⚠️ DESDE EL FLIP DEL 2026-08-31, EL MODO POR DEFECTO **NO** ES PRODUCCIÓN
#   Producción corre **Python 3.10.12 + Django 5.2.17** (venv /root/venv-integrador-52).
#   El modo por defecto de este script usa el **Python 3.7 del sistema + Django 3.2.18**,
#   que hoy es el **stack de ROLLBACK**, no el de producción. Sigue siendo útil —prueba
#   que el código puede volver atrás— pero **no** valida lo que corre hoy.
#   Para validar producción de verdad: `PYTHON=`, abajo. Necesita root.
#
# POR QUÉ EXISTE
#   Local y producción divergen, y un "todo en verde" local NO prueba compatibilidad
#   con el servidor. Esa suposición ya falló una vez (2026-08-25).
#
# CORRE EN EL SERVIDOR, PERO NO TOCA PRODUCCIÓN
#   Usa el Python 3.7 del sistema, no el venv, y `muci-integrador/test_settings.py`
#   es autónomo: SQLite en memoria, hosts de BIMS/WooCommerce inventados, el `.env`
#   nunca se lee. Los tests además interceptan `HTTPAdapter.send`, así que no sale
#   un paquete de la máquina. El checkout de `/var/www/integrador` no se toca y el
#   servicio no se entera. El directorio temporal se borra siempre, aunque los
#   tests fallen.
#
# QUÉ PRUEBA CADA MODO
#   Por defecto (3.7 del sistema + Django 3.2.18): compatibilidad con el stack de
#   ROLLBACK. Corre como `anthropic_readonly`, sin root.
#   Con `PYTHON=/root/venv-integrador-52/bin/python`: el stack REAL de producción.
#   Necesita root, así que esa corrida la hace Carlos.
#
# USO
#   ./verificar-en-stack-produccion.sh            # verifica HEAD
#   ./verificar-en-stack-produccion.sh <rama>     # verifica otra ref
#
#   Sobre el venv REAL de producción (Python 3.10 + Django 5.2), con root:
#     PYTHON=/root/venv-integrador-52/bin/python \
#       SERVIDOR=root@muci.org REMOTO=wt-verificacion-52 \
#       ./verificar-en-stack-produccion.sh
#   Corrido así el 2026-08-31 sobre 348fd83: 179/179 en 3.10.12 + Django 5.2.17.
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
    echo "==> VERDE sobre $PYTHON en $SERVIDOR ($COMMIT)"
else
    echo "==> ROJO sobre $PYTHON en $SERVIDOR ($COMMIT) — código $CODIGO" >&2
fi

exit "$CODIGO"
