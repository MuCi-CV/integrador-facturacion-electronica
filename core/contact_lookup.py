import re
from typing import Optional

from core.models import ContactCache
from core.ruc import get_razon_social


def _normalize_document(raw: str) -> tuple:
    """
    (base_number, clean_document_id) a partir de un RUC/documento crudo.
    base_number: solo dígitos de la parte anterior al guión ("2109835-2" -> "2109835").
    clean_document_id: base + verificador si existe; si no, base.
    """
    raw = (raw or "").strip()
    base_number = re.sub(r"\D", "", raw.split("-")[0])
    verificador = raw.split("-")[1] if "-" in raw else None
    clean_document_id = f"{base_number}-{verificador}" if verificador else base_number
    return base_number, clean_document_id


def lookup_contact(ruc: Optional[str] = None, email: Optional[str] = None) -> dict:
    """
    Resuelve datos de un cliente por RUC o por email.
    Retorna {"razon_social", "email", "documento", "ruc", "source"}.
    source ∈ {"contactcache", "ruc", "none"}. Nunca lanza.
    """
    result = {
        "razon_social": None,
        "email": None,
        "documento": None,
        "ruc": None,
        "source": "none",
    }

    if ruc:
        base_number, clean_document_id = _normalize_document(ruc)
        result["ruc"] = clean_document_id or None
        result["documento"] = base_number or None

        contact = (
            ContactCache.objects.filter(document_id=base_number).first()
            or ContactCache.objects.filter(document_id=clean_document_id).first()
        )
        if contact:
            result["email"] = contact.email
            result["source"] = "contactcache"

        razon = get_razon_social(clean_document_id)
        if razon:
            result["razon_social"] = razon
            if result["source"] == "none":
                result["source"] = "ruc"
        return result

    if email:
        email = email.strip()
        result["email"] = email
        contact = ContactCache.objects.filter(email=email).first()
        if contact:
            result["source"] = "contactcache"
            base_number, clean_document_id = _normalize_document(contact.document_id or "")
            result["documento"] = base_number or None
            result["ruc"] = clean_document_id or None
            if clean_document_id:
                razon = get_razon_social(clean_document_id)
                if razon:
                    result["razon_social"] = razon
        return result

    return result
