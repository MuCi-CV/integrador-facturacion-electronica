import logging
from datetime import timedelta
from typing import Optional

import requests
from django.conf import settings
from django.db import IntegrityError
from django.utils.timezone import now

from core.models import RucCache

logger = logging.getLogger("ruc_api")

CACHE_TTL_DAYS = 30


def _fetch_from_api(ruc: str, timeout: int = 5) -> Optional[str]:
    """
    Pega a la fuente externa (turuc). Devuelve la razón social si responde
    positivo; None ante fuente no configurada, error de red/timeout, JSON
    inválido, sin match o razón social vacía. Nunca lanza hacia el caller.
    """
    base_url = getattr(settings, "RUC_API_URL", None)
    if not base_url or not ruc:
        return None
    try:
        res = requests.get(f"{base_url}/api/contribuyente/{ruc}", timeout=timeout)
        res.raise_for_status()
        payload = res.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Consulta RUC {ruc} falló: {e}")
        return None
    razon_social = (payload.get("data") or {}).get("razonSocial")
    if razon_social and razon_social.strip():
        return razon_social.strip()
    return None


def get_razon_social(ruc: str, timeout: int = 5) -> Optional[str]:
    """
    Resuelve la razón social de un RUC usando RucCache (TTL 30 días) y, si hace
    falta, la fuente externa.

    - Caché fresco (<30 días): devuelve el valor cacheado, sin llamar a la API.
    - Caché vencido/ausente + API ok: devuelve el valor nuevo y refresca checked_at.
    - Caché vencido + API falla: devuelve el valor viejo SIN renovar checked_at.
    - Sin caché + API falla: devuelve None (el caller cae a WooCommerce).
    """
    if not ruc:
        return None

    cached = RucCache.objects.filter(ruc=ruc).first()

    if cached and (now() - cached.checked_at) < timedelta(days=CACHE_TTL_DAYS):
        return cached.razon_social

    fetched = _fetch_from_api(ruc, timeout)
    if fetched:
        try:
            RucCache.objects.update_or_create(
                ruc=ruc, defaults={"razon_social": fetched, "checked_at": now()}
            )
        except IntegrityError:
            # Otro request concurrente ya insertó este RUC; sin efecto.
            pass
        return fetched

    if cached:
        return cached.razon_social
    return None
