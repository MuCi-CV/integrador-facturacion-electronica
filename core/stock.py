"""
Lógica de la sincronización de stock BIMS → WooCommerce.

Todo acá es **puro**: sin red, sin ORM, sin Django. El comando `sync_stock` es el
único que hace I/O. Esa separación es deliberada — las decisiones que pueden
apagar la tienda se prueban sin levantar nada.

No importa `core.services` ni `core.bims`: `bims.py` instancia `BimsApi()` en el
import y hace login. Es la misma razón por la que `core/states.py` vive aparte.
"""

import re
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

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


SKU_SIN_VINCULO = "sin_vinculo"
SKU_AMBIGUO = "ambiguo"
SKU_DADO_DE_BAJA = "dado_de_baja"


def resolver_bims_id(
    sku_propio: Optional[str],
    sku_padre: Optional[str],
    hermanas_sin_sku: int,
) -> Tuple[Optional[int], Optional[str]]:
    """
    El id de BIMS de un producto de Woo, o el motivo por el que no se puede.

    Devuelve `(bims_id, None)` cuando hay vínculo y `(None, motivo)` cuando no.
    `hermanas_sin_sku` es cuántas variaciones publicadas del mismo padre están
    sin SKU propio, **contándose a sí misma**.

    La herencia del padre es la semántica de WooCommerce, pero sólo vale cuando
    la variación es la única sin SKU. Con varias, todas heredarían el mismo id de
    BIMS y se escribiría el mismo stock N veces: inventario multiplicado.
    """
    try:
        propio = bims_product_id(sku_propio)
    except SkuDadoDeBaja:
        return None, SKU_DADO_DE_BAJA
    if propio is not None:
        return propio, None

    if hermanas_sin_sku > 1:
        return None, SKU_AMBIGUO

    try:
        heredado = bims_product_id(sku_padre)
    except SkuDadoDeBaja:
        return None, SKU_DADO_DE_BAJA
    if heredado is not None:
        return heredado, None

    return None, SKU_SIN_VINCULO


class Cambio(NamedTuple):
    """
    Una escritura pendiente a WooCommerce.

    `ruta_woo` es lo que va después de `products/`: `"100"` para un producto
    simple y `"187056/variations/188079"` para una variación, porque WooCommerce
    escribe las variaciones en un endpoint anidado.
    """

    woo_id: int
    ruta_woo: str
    bims_id: int
    stock_actual: float
    stock_nuevo: float
    apaga: bool


def calcular_cambios(candidatos: list, stock_por_bims_id: dict) -> List[Cambio]:
    """
    Las escrituras necesarias, y sólo ésas.

    Un producto que **no está** en `stock_por_bims_id` no se toca: significa que
    BIMS no lo devolvió, y eso es "no hay dato", no "el dato es cero". Es la
    guarda que evita que una lectura fallida apague el catálogo público.
    """
    cambios = []

    for candidato in candidatos:
        bims_id = candidato["bims_id"]
        if bims_id not in stock_por_bims_id:
            continue

        nuevo = float(stock_por_bims_id[bims_id])
        actual = float(candidato["stock_actual"])
        if nuevo == actual:
            continue

        cambios.append(
            Cambio(
                woo_id=candidato["woo_id"],
                ruta_woo=candidato["ruta_woo"],
                bims_id=bims_id,
                stock_actual=actual,
                stock_nuevo=nuevo,
                apaga=nuevo <= 0 < actual,
            )
        )

    return cambios


def radio_excedido(cambios: Iterable[Cambio], tope: int) -> int:
    """
    Cuántos productos se apagarían, si eso pasa el tope. `0` si está dentro.

    Sólo cuenta los que **apagan**: el primer barrido real va a encender decenas
    de productos que hoy figuran agotados teniendo stock, y eso es el resultado
    esperado, no una anomalía.
    """
    apagados = sum(1 for c in cambios if c.apaga)
    return apagados if apagados > tope else 0
