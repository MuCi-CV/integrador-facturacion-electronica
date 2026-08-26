"""
Presupuesto de tiempo por orden, transportado con un `contextvars.ContextVar`.

gunicorn corre con `--timeout 120` y mata al worker **por señal** al pasarse. Un
worker matado por señal no ejecuta el `except` que graba el `FailedOrder`, así
que la orden desaparece sin factura y sin registro. El presupuesto por llamada de
`bims.PRESUPUESTO_REINTENTOS` no alcanza: una orden hace 2-3 llamadas a BIMS más
un `get_product` por ítem, y nadie mira la suma.

Se usa un ContextVar y no un parámetro por firma porque un call site nuevo que se
olvide de pasarlo quedaría sin límite **en silencio** — el mismo tipo de falla que
esto viene a arreglar. Y no un atributo del singleton `bims` porque sería estado
mutable global sobre un objeto compartido, que se rompe sin aviso el día que algo
sea concurrente.
"""

import contextvars
import time
from typing import Optional

# 90 s deja 30 s de margen contra los --timeout 120 de gunicorn: suficiente para
# grabar el FailedOrder y responder. Una orden normal tarda 10-20 s, así que en
# operación sana esto no debería activarse nunca.
PRESUPUESTO_ORDEN = 90

_deadline = contextvars.ContextVar("deadline_orden", default=None)


class PresupuestoOrdenAgotado(Exception):
    """
    La orden superó su presupuesto total. Terminal para esta corrida, reintentable
    desde el admin.

    Hereda de `Exception` a propósito, NO de `BimsTransientError`: tiene que pasar
    por encima del `except BimsTransientError` de `_retry_request`, o el reintento
    se la comería justo cuando ya no queda tiempo para reintentar.
    """


def iniciar(presupuesto: float = PRESUPUESTO_ORDEN) -> contextvars.Token:
    """Arranca el reloj. Devuelve un token para restaurar en un `finally`."""
    return _deadline.set(time.monotonic() + presupuesto)


def restaurar(token: contextvars.Token) -> None:
    """Deshace un `iniciar()`. Va siempre en un `finally`."""
    _deadline.reset(token)


def restante() -> Optional[float]:
    """
    Segundos que quedan, o `None` si no hay presupuesto fijado en este contexto.

    El `None` es deliberado y es lo que protege al cron `sync_bims_contacts`, que
    hace 38 llamadas secuenciales y nunca debe tener deadline de orden. No se
    recorta a 0: el consumidor necesita distinguir "agotado" (negativo) de "sin
    presupuesto" (None).
    """
    limite = _deadline.get()
    return None if limite is None else limite - time.monotonic()
