"""
Avisos a Slack.

Sentry queda para bugs de código; Slack para transacciones que no llegaron. La
distinción importa: hoy cada `logger.error()` es un evento de Sentry, así que
usar Sentry para "la cola creció" mezcla un problema de operación con un problema
de programación y arruina las dos señales.

Todo acá es **fail-safe**: un problema al avisar nunca debe romper la
facturación. Avisar es lo secundario; facturar es lo que importa.
"""

import logging
from datetime import timedelta
from typing import Optional

import requests
from django.conf import settings
from django.utils.timezone import now

from core.models import AlertThrottle

logger = logging.getLogger(__name__)

TIMEOUT = 5

# Cuánto se calla una misma clase de alerta después de avisar. Una caída de BIMS
# de una hora tiene que dar 4 mensajes, no 60.
THROTTLE_MINUTES = 15


def notify(clave: str, texto: str) -> None:
    """
    Avisa a Slack una vez por clave y por ventana de silencio.

    La marca de "ya avisé" se escribe **después** de que Slack conteste, no
    antes: si Slack está caído, el aviso no se da por hecho y se vuelve a
    intentar en la pasada siguiente. El costo de esa elección está acotado por la
    cadencia del cron (un intento por minuto, con timeout de 5 s).
    """
    url: Optional[str] = getattr(settings, "SLACK_WEBHOOK_URL", "")
    if not url:
        return

    if _silenciado(clave):
        return

    try:
        requests.post(url, json={"text": texto}, timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"No se pudo avisar a Slack ({clave}): {e}")
        return

    _marcar(clave)


def _silenciado(clave: str) -> bool:
    limite = now() - timedelta(minutes=THROTTLE_MINUTES)
    return AlertThrottle.objects.filter(clave=clave, sent_at__gt=limite).exists()


def _marcar(clave: str) -> None:
    AlertThrottle.objects.update_or_create(clave=clave, defaults={"sent_at": now()})
