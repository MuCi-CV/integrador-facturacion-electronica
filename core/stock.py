"""
Lógica de la sincronización de stock BIMS → WooCommerce.

Todo acá es **puro**: sin red, sin ORM, sin Django. El comando `sync_stock` es el
único que hace I/O. Esa separación es deliberada — las decisiones que pueden
apagar la tienda se prueban sin levantar nada.

No importa `core.services` ni `core.bims`: `bims.py` instancia `BimsApi()` en el
import y hace login. Es la misma razón por la que `core/states.py` vive aparte.
"""

import re
from collections import Counter
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
SKU_DADO_DE_BAJA = "dado_de_baja"

#: Un producto `private` **sí** se vende: es el catálogo del POS de FooEvents
#: (`fooeventspos_variation_show_in_pos = yes`). `draft` y `trash`, no.
ESTADOS_VENDIBLES = ("publish", "private")


def sku_propio(sku_variacion: Optional[str], sku_padre: Optional[str]) -> Optional[str]:
    """
    El SKU **propio** de una variación, o `None` si no tiene y está heredando.

    ⚠️ La REST de WooCommerce **no distingue las dos situaciones**:
    `WC_Product_Variation::get_sku()` devuelve el del padre cuando la variación no
    tiene propio, así que "tiene el 575" y "hereda el 575" llegan idénticos.

    Se resuelve comparando contra el del padre, y eso es válido por un invariante
    del catálogo **medido el 2026-09-03**: no hay ningún SKU propio numérico
    repetido entre productos y variaciones. Si son iguales, es heredado.
    """
    propio = str(sku_variacion or "").strip()
    if not propio:
        return None
    if propio == str(sku_padre or "").strip():
        return None
    return propio


class Destino(NamedTuple):
    """
    Un producto de BIMS y el **único** lugar de Woo donde se escribe su stock.

    `gestiona_stock` es el estado actual de `manage_stock` en Woo: cuando es
    `False`, publicar el stock convierte ese producto de ilimitado a limitado, y
    eso hay que informarlo antes de aplicarlo.
    """

    woo_id: int
    ruta_woo: str
    bims_id: int
    stock_actual: float
    gestiona_stock: bool
    etiqueta: str


class VariacionIgnorada(NamedTuple):
    """
    Una variación que hereda el SKU del padre **pero gestiona su propio stock**.

    WooCommerce usa el contador de la variación, así que el número que se escriba
    en el padre no limita su venta. Son 22 (8 padres, todo merch) medidas el
    2026-09-03. Decisión de Carlos: reportarlas y **no tocarlas** — arreglarlas
    implica apagarles `manage_stock` en Woo, que es un cambio de datos.
    """

    woo_id: int
    padre_id: int
    bims_id: int
    stock_actual: float
    etiqueta: str


def _stock_actual(entrada: dict) -> float:
    return _a_float(entrada.get("stock_quantity"))


def gestiona_su_stock(manage_stock) -> bool:
    """
    Si esa entrada de Woo lleva su **propio** contador de stock.

    ⚠️ La REST devuelve **tres** valores en el `manage_stock` de una variación:
    `true`, `false` y el string **`"parent"`**. Ese string significa "no
    gestiono, lo hace mi padre", y aparece exactamente cuando el padre sí
    gestiona — o sea el caso en que escribir en el padre **sí** limita la venta.

    `bool("parent")` es `True`, así que tomar el valor crudo **invierte el
    sentido**: marca como problema las variaciones que están bien. Medido el
    2026-09-03 en el barrido en seco: 57 reportadas contra 22 reales, unos 35
    falsos positivos, todos de eventos cuyo padre gestiona el stock.
    """
    if isinstance(manage_stock, str):
        return manage_stock.strip().lower() not in ("parent", "no", "false", "")
    return bool(manage_stock)


def destinos_de_producto(
    producto: dict, variaciones: Optional[list]
) -> Tuple[List[Destino], "Counter", List[VariacionIgnorada]]:
    """
    Los destinos de escritura de un producto de Woo: **uno por producto de BIMS**.

    La regla es la de Carlos (2026-09-03), y refleja cómo se crea el catálogo: hay
    un producto de BIMS por producto simple o por variación. Entonces una
    variación con SKU propio **es** un producto de BIMS y recibe su stock; una que
    hereda pertenece al producto del padre, y en ese caso el destino es **el
    padre, una sola vez**.

    Reemplaza la herencia por variación, que fabricaba vínculos: `188079` y
    `188080` no tienen SKU propio y recibían cada una el stock del `575`, o sea
    32 unidades publicadas donde hay 16.
    """
    descartes: Counter = Counter()
    destinos: List[Destino] = []
    ignoradas: List[VariacionIgnorada] = []

    if producto.get("status") not in ESTADOS_VENDIBLES:
        return destinos, descartes, ignoradas

    sku_del_padre = producto.get("sku")
    padre_dado_de_baja = False
    try:
        bims_id_padre = bims_product_id(sku_del_padre)
    except SkuDadoDeBaja:
        bims_id_padre = None
        padre_dado_de_baja = True
        descartes[SKU_DADO_DE_BAJA] += 1

    if not variaciones:
        if bims_id_padre is not None:
            destinos.append(
                Destino(
                    woo_id=producto["id"],
                    ruta_woo=str(producto["id"]),
                    bims_id=bims_id_padre,
                    stock_actual=_stock_actual(producto),
                    gestiona_stock=gestiona_su_stock(producto.get("manage_stock")),
                    etiqueta=producto.get("name") or "",
                )
            )
        elif not padre_dado_de_baja:
            # Un SKU de baja ya se contó arriba: es un motivo, no dos.
            descartes[SKU_SIN_VINCULO] += 1
        return destinos, descartes, ignoradas

    hereda_alguien = False

    for variacion in variaciones:
        if variacion.get("status") not in ESTADOS_VENDIBLES:
            continue

        try:
            propio = bims_product_id(sku_propio(variacion.get("sku"), sku_del_padre))
        except SkuDadoDeBaja:
            descartes[SKU_DADO_DE_BAJA] += 1
            continue

        if propio is not None:
            destinos.append(
                Destino(
                    woo_id=variacion["id"],
                    ruta_woo=f"{producto['id']}/variations/{variacion['id']}",
                    bims_id=propio,
                    stock_actual=_stock_actual(variacion),
                    gestiona_stock=gestiona_su_stock(variacion.get("manage_stock")),
                    etiqueta=variacion.get("name") or "",
                )
            )
            continue

        # Hereda del padre: el dueño del stock es el padre, no ella.
        if bims_id_padre is None:
            descartes[SKU_SIN_VINCULO] += 1
            continue

        hereda_alguien = True
        if gestiona_su_stock(variacion.get("manage_stock")):
            ignoradas.append(
                VariacionIgnorada(
                    woo_id=variacion["id"],
                    padre_id=producto["id"],
                    bims_id=bims_id_padre,
                    stock_actual=_stock_actual(variacion),
                    etiqueta=variacion.get("name") or "",
                )
            )

    if hereda_alguien and bims_id_padre is not None:
        destinos.append(
            Destino(
                woo_id=producto["id"],
                ruta_woo=str(producto["id"]),
                bims_id=bims_id_padre,
                stock_actual=_stock_actual(producto),
                gestiona_stock=gestiona_su_stock(producto.get("manage_stock")),
                etiqueta=producto.get("name") or "",
            )
        )

    return destinos, descartes, ignoradas


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
    gestiona_stock: bool
    etiqueta: str


def calcular_cambios(
    destinos: Iterable[Destino], stock_por_bims_id: dict
) -> List[Cambio]:
    """
    Las escrituras necesarias, y sólo ésas.

    Un producto que **no está** en `stock_por_bims_id` no se toca: significa que
    BIMS no lo devolvió, y eso es "no hay dato", no "el dato es cero". Es la
    guarda que evita que una lectura fallida apague el catálogo público.
    """
    cambios = []

    for destino in destinos:
        if destino.bims_id not in stock_por_bims_id:
            continue

        nuevo = float(stock_por_bims_id[destino.bims_id])
        actual = float(destino.stock_actual)
        if nuevo == actual:
            continue

        cambios.append(
            Cambio(
                woo_id=destino.woo_id,
                ruta_woo=destino.ruta_woo,
                bims_id=destino.bims_id,
                stock_actual=actual,
                stock_nuevo=nuevo,
                apaga=nuevo <= 0 < actual,
                gestiona_stock=destino.gestiona_stock,
                etiqueta=destino.etiqueta,
            )
        )

    return cambios


def colisiones(destinos: Iterable[Destino]) -> Dict[int, List[Destino]]:
    """
    Productos de BIMS reclamados por más de un destino, agrupados por id.

    Con el modelo de "un producto de BIMS, un destino" esto **no debería pasar
    nunca**, y por eso existe: si pasa, hay un SKU propio repetido en Woo —el
    invariante medido roto— y publicar el mismo stock N veces multiplica el
    inventario. Es la red de seguridad del bug del 2026-09-03, no su arreglo: el
    arreglo es que el padre sea un único destino.
    """
    por_bims_id: Dict[int, List[Destino]] = {}
    for destino in destinos:
        por_bims_id.setdefault(destino.bims_id, []).append(destino)
    return {bims_id: ds for bims_id, ds in por_bims_id.items() if len(ds) > 1}


def radio_excedido(cambios: Iterable[Cambio], tope: int) -> int:
    """
    Cuántos productos se apagarían, si eso pasa el tope. `0` si está dentro.

    Sólo cuenta los que **apagan**: el primer barrido real va a encender decenas
    de productos que hoy figuran agotados teniendo stock, y eso es el resultado
    esperado, no una anomalía.
    """
    apagados = sum(1 for c in cambios if c.apaga)
    return apagados if apagados > tope else 0
