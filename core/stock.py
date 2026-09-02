"""
Lógica de la sincronización de stock BIMS → WooCommerce.

Todo acá es **puro**: sin red, sin ORM, sin Django. El comando `sync_stock` es el
único que hace I/O. Esa separación es deliberada — las decisiones que pueden
apagar la tienda se prueban sin levantar nada.

No importa `core.services` ni `core.bims`: `bims.py` instancia `BimsApi()` en el
import y hace login. Es la misma razón por la que `core/states.py` vive aparte.
"""

import re
from typing import Optional

_SKU_NUMERICO = re.compile(r"^\d+$")


class SkuDadoDeBaja(Exception):
    """
    El SKU tiene la forma de un producto dado de baja.

    Convención de Carlos: al dar de baja un producto se le cambia el SKU
    numérico por `<id>-<n>`, donde `n` es cuántas veces se dio de baja (`7` pasa
    a `7-1`, después a `7-5`, etc.). No es un dato corrupto, es un estado.

    Se levanta en vez de devolver `None` porque los dos consumidores lo tratan
    distinto y la diferencia importa: al **facturar** tiene que hacer fallar la
    orden (decisión de Carlos), y en el **barrido de stock** se saltea ese
    producto. Un `None` indistinguible de "sin SKU" borraría esa distinción.
    """


def bims_product_id(sku: Optional[str]) -> Optional[int]:
    """
    El id de producto de BIMS que corresponde a un SKU de WooCommerce.

    `None` significa "no hay vínculo": SKU vacío, o el `0` que deja un campo sin
    llenar. Un SKU no numérico levanta `SkuDadoDeBaja`.
    """
    if sku is None:
        return None

    limpio = str(sku).strip()
    if not limpio:
        return None

    if not _SKU_NUMERICO.match(limpio):
        raise SkuDadoDeBaja(
            f"SKU {limpio!r}: el producto está dado de baja (los SKU de baja "
            f"tienen la forma <id>-<n>). No corresponde facturarlo."
        )

    valor = int(limpio)
    return valor or None
