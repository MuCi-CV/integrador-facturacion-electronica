"""
Transiciones de estado de `FailedOrder`, sin dependencias de red.

Vive aparte de `services.py` a propósito: `services` importa `core.bims`, que
instancia `BimsApi()` en el import y **dispara un login real contra BIMS**. Un
helper de estado no tiene por qué arrastrar eso — es lo que hace que
`makemigrations` con `test_settings` crashee.
"""

from typing import Any

from core.models import FailedOrder


def _como_order_id(referencia: Any) -> int:
    """
    Durante la expansión `order_id` sigue siendo NOT NULL, así que una referencia
    no numérica no se puede persistir. Falla acá con un mensaje claro en vez de
    dar un `IntegrityError` de MariaDB tres capas más abajo.
    """
    try:
        return int(referencia)
    except (TypeError, ValueError):
        raise ValueError(
            f"Referencia no numérica: {referencia!r}. Mientras `order_id` siga "
            "existiendo (fase de expansión) solo se admiten referencias de "
            "WooCommerce. El origen CRM entra con el sub-proyecto F, después de "
            "la contracción."
        )


def upsert_state(referencia: Any, **campos: Any) -> FailedOrder:
    """
    Único lugar que escribe la identidad de una fila de `FailedOrder`.

    Existe para que sea imposible llenar una columna de identidad y no la otra
    durante la expansión: una fila con solo `order_id` no la encuentra el código
    nuevo, y una con solo `external_reference` no la encuentra el viejo.

    El rescate por `order_id` no es defensivo de más. En el despliegue, `migrate`
    corre con el código VIEJO todavía sirviendo: una venta que entre entre el fin
    de la migración y el `restart` deja una fila con `external_reference` en
    NULL. Sin ese segundo intento, el código nuevo no la vería y crearía una
    **segunda fila para la misma orden**, que es justo la doble fuente de verdad
    sobre si una orden se facturó que esta tabla existe para evitar.
    """
    order_id = _como_order_id(referencia)
    referencia = str(referencia)

    fila = FailedOrder.objects.filter(
        origin=FailedOrder.ORIGIN_WOO, external_reference=referencia
    ).first()
    if fila is None:
        fila = FailedOrder.objects.filter(
            origin=FailedOrder.ORIGIN_WOO,
            external_reference__isnull=True,
            order_id=order_id,
        ).first()

    if fila is None:
        return FailedOrder.objects.create(
            origin=FailedOrder.ORIGIN_WOO,
            external_reference=referencia,
            order_id=order_id,
            **campos,
        )

    fila.external_reference = referencia
    fila.order_id = order_id
    for nombre, valor in campos.items():
        setattr(fila, nombre, valor)
    # `updated_at` es `auto_now`: con `update_fields` explícito, Django no lo
    # toca si no está en la lista.
    fila.save(update_fields=["external_reference", "order_id", *campos, "updated_at"])
    return fila
