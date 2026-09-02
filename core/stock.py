"""
Lógica de la sincronización de stock BIMS → WooCommerce.

Todo acá es **puro**: sin red, sin ORM, sin Django. El comando `sync_stock` es el
único que hace I/O. Esa separación es deliberada — las decisiones que pueden
apagar la tienda se prueban sin levantar nada.

No importa `core.services` ni `core.bims`: `bims.py` instancia `BimsApi()` en el
import y hace login. Es la misma razón por la que `core/states.py` vive aparte.
"""

import re
from typing import Dict, Iterable, Optional

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


def _a_float(valor) -> float:
    """
    BIMS manda el mismo número como `'0'`, `'0.000000'` y
    `'128.0000000000000000'` en una sola respuesta, así que comparar como texto
    da falsos negativos. Un valor ilegible cuenta como 0: no vale tirar un
    barrido completo por una celda rara.
    """
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def desglose_por_deposito(
    availability_full: Optional[list], depositos: Iterable[int]
) -> Dict[int, float]:
    """
    Cuánto aporta cada depósito habilitado, omitiendo los que aportan 0.

    Existe para la auditoría, y no es prolijidad: **WooCommerce no sabe en qué
    depósito vive nada, sólo BIMS lo sabe**, así que cuando alguien pregunte "por
    qué la web dice 3" la respuesta no está ni en la web ni en Woo. Sin este
    desglose en el registro, ese número no se puede explicar después.

    Un depósito en negativo se trata como 0: un desajuste de inventario en un
    depósito no es una deuda que haya que descontarle a otro.
    """
    habilitados = {int(d) for d in depositos}
    salida: Dict[int, float] = {}

    for entrada in availability_full or []:
        fila = entrada.get("Availability", entrada) if isinstance(entrada, dict) else {}
        try:
            deposito = int(fila.get("warehouse_id"))
        except (TypeError, ValueError):
            continue
        if deposito not in habilitados:
            continue
        unidades = max(0.0, _a_float(fila.get("total")))
        if unidades:
            salida[deposito] = salida.get(deposito, 0.0) + unidades

    return salida


def stock_vendible(
    availability_full: Optional[list], depositos: Iterable[int]
) -> float:
    """
    Unidades vendibles de un producto: la suma de `total` sobre los depósitos
    habilitados.

    Se suma `total` y no `total2` porque `Product.availability` —el agregado que
    calcula BIMS— es la suma de `total`. `total2` trae negativos proporcionales
    al volumen de venta: es una salida acumulada.
    """
    return sum(desglose_por_deposito(availability_full, depositos).values())
