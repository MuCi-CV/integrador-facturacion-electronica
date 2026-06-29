import logging
from typing import Optional

import requests
from django.conf import settings

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
