"""
Transiciones de estado de `FailedOrder`, sin dependencias de red.

Vive aparte de `services.py` a propósito: `services` importa `core.bims`, que
instancia `BimsApi()` en el import y **dispara un login real contra BIMS**. Un
helper de estado no tiene por qué arrastrar eso — es lo que hace que
`makemigrations` con `test_settings` crashee.
"""

from typing import Any, Optional

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


def _buscar_fila(
    order_id: int, referencia: str, origen: str
) -> Optional[FailedOrder]:
    """
    La fila de esta transacción, o `None` si todavía no existe.

    El rescate por `order_id` no es defensivo de más. En el despliegue, `migrate`
    corre con el código VIEJO todavía sirviendo: una venta que entre entre el fin
    de la migración y el `restart` deja una fila con `external_reference` en
    NULL. Sin ese segundo intento, el código nuevo no la vería y crearía una
    **segunda fila para la misma orden**, que es justo la doble fuente de verdad
    sobre si una orden se facturó que esta tabla existe para evitar.
    """
    fila = FailedOrder.objects.filter(
        origin=origen, external_reference=referencia
    ).first()
    if fila is None:
        fila = FailedOrder.objects.filter(
            origin=origen,
            external_reference__isnull=True,
            order_id=order_id,
        ).first()
    return fila


def upsert_state(
    referencia: Any, origen: str = FailedOrder.ORIGIN_WOO, **campos: Any
) -> FailedOrder:
    """
    Único lugar que escribe la identidad de una fila de `FailedOrder`.

    Existe para que sea imposible llenar una columna de identidad y no la otra
    durante la expansión: una fila con solo `order_id` no la encuentra el código
    nuevo, y una con solo `external_reference` no la encuentra el viejo.
    """
    order_id = _como_order_id(referencia)
    referencia = str(referencia)

    fila = _buscar_fila(order_id, referencia, origen)

    if fila is None:
        return FailedOrder.objects.create(
            origin=origen,
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


def mark_not_applicable(referencia: Any, motivo: str) -> FailedOrder:
    """
    La transacción no corresponde facturar. Estado TERMINAL, sin reintento.

    Antes estas órdenes salían por un `return` temprano sin dejar rastro, y esa
    ausencia era ambigua: una orden sin `_bims_sale_id` podía ser "no
    correspondía facturar" o "se perdió en el camino". Con el CRM entrando como
    segundo origen, esa ambigüedad se vuelve una respuesta equivocada a la única
    pregunta que el CRM va a hacer.

    Pasa por `upsert_state` y no por `update_or_create` a propósito: `order_id`
    sigue siendo NOT NULL durante la expansión, así que escribir solo
    `external_reference` da `IntegrityError` al crear la fila.
    """
    return upsert_state(
        referencia, status=FailedOrder.NOT_APPLICABLE, message=motivo
    )


# Estados desde los que una re-entrega vuelve a encolar. `COMPLETED` queda
# afuera porque ya se facturó y reprocesar es riesgo sin beneficio; `PENDING` y
# `PROCESSING` porque ya están en la cola. Spec §4.
#
# `PAUSED` entra aunque el plan no lo listaba: no es "ya se hizo" ni "ya está en
# la cola", es una orden trabada esperando que aparezca un contacto. Dejarla
# afuera volvía imposible el reintento que hace `sync_bims_contacts`, que es
# justo para lo que ese estado existe.
REQUEUEABLE = (
    FailedOrder.FAILED,
    FailedOrder.NOT_APPLICABLE,
    FailedOrder.PAUSED,
)


def enqueue(referencia: Any, origen: str = FailedOrder.ORIGIN_WOO) -> FailedOrder:
    """
    Deja la transacción lista para que la tome el worker, y no hace nada más.

    `NOT_APPLICABLE` es reencolable a propósito: es terminal para el worker, no
    para una entrega nueva. Si a una orden de monto 0 le corrigen el precio, Woo
    reentrega el webhook y esta vez sí corresponde facturar.

    El reinicio de `bims_attempts` tampoco es cosmético: sin él, una orden que ya
    agotó su presupuesto de reintentos quedaría encolada y el worker la
    descartaría en el acto, así que una re-entrega manual no serviría de nada.
    """
    fila = _buscar_fila(_como_order_id(referencia), str(referencia), origen)

    if fila is not None and fila.status not in REQUEUEABLE:
        return fila

    return upsert_state(
        referencia,
        origen=origen,
        status=FailedOrder.PENDING,
        message="Reencolada." if fila is not None else "Encolada.",
        bims_attempts=0,
        bims_next_attempt=None,
    )
