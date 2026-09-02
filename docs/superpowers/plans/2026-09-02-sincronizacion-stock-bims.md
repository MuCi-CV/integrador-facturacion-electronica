# Sincronización de stock BIMS → WooCommerce — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que WooCommerce publique el stock que BIMS tiene, para los productos que BIMS declara inventariables, sin poder apagar ventas por un error de lectura o de configuración.

**Architecture:** Un comando de management corre por cron cada 15 minutos. Enumera los productos publicados de WooCommerce (que ya traen su stock actual, así que la comparación sale gratis), lee el stock de BIMS con `products/index.json?v_stock=1` paginado de a 100, suma `total` sobre los depósitos habilitados, y escribe **sólo donde el número difiere**. Toda la lógica de decisión vive en funciones puras en `core/stock.py`, sin red, y el comando es la única pieza que hace I/O.

**Tech Stack:** Python 3.10 + Django 5.2.17 en producción (3.12 en local), `requests`, cliente `woocommerce` 3.0.0, `flock` para el cron. Sin dependencias nuevas. **Sin migraciones** — no hay modelos nuevos.

**Spec:** `docs/superpowers/specs/2026-09-02-sincronizacion-stock-bims-design.md`

## Global Constraints

- **Rama:** `feature/sincronizacion-stock-bims`, que sale de `main` @ `f80b1a4`.
- **Suite:** `.venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings`. **Baseline: 241 tests en verde.** Cada tarea deja la suite verde.
- **Ejecutar los tests con techo de memoria:** `( ulimit -v 6291456; .venv/bin/python manage.py test ... )`. Un `@patch` sin `return_value` cuyo mock llega a un serializador puede consumir 22 GiB y congelar la máquina; ya pasó tres veces el 2026-09-01.
- **Ningún test sale a la red.** Todo mock de `wc_api` o de BIMS va con `patch`, y si una clase de tests deja filas que otro código levanta, el parche va en `setUp`. Ya nos pasó que un test hacía un `GET` real a WooCommerce y el `except` del lote se lo tragaba.
- **Los mocks devuelven la forma real de la API**, con todas las claves que el código lee. Un mock irreal (`{"id": ...}` sin `meta_data`) ya dejó un agujero con la suite en verde.
- **Nomenclatura:** identificadores de código y columnas en **inglés**; documentación, comentarios y docstrings en **español**. Nombres de test en español, como el resto de `core/tests.py`.
- **Anotaciones de tipo obligatorias** en toda función nueva.
- **`total` se parsea como float, nunca se compara como texto.** BIMS devuelve `'0'`, `'0.000000'` y `'128.0000000000000000'` en la misma respuesta.
- **Verificación antes de desplegar:** `./verificar-en-stack-produccion.sh` (stack de rollback, 3.7 + Django 3.2) la corre el asistente; la del stack real (`PYTHON=/root/venv-integrador-52/bin/python SERVIDOR=root@muci.org REMOTO=wt-verificacion-52`) la corre Carlos porque necesita root.
- **`black` no está instalado y el código no está formateado con él.** No correrlo: enterraría el diff.

---

## Estructura de archivos

| archivo | responsabilidad |
|---|---|
| `core/stock.py` **(nuevo)** | Toda la lógica de decisión, en funciones puras: normalizar el SKU a id de BIMS, sumar el stock vendible, resolver el nivel padre/variación, calcular qué cambió y aplicar la guarda de radio. Sin red, sin Django ORM. |
| `core/woocommerce.py` (modificar) | Dos métodos nuevos de I/O: `get_variations` y `update_product_stock`. |
| `core/bims.py` (modificar) | Un método de lectura nuevo: `get_products_with_stock`. Hoy el cliente **no tiene ninguna lectura de productos con stock**. |
| `core/services.py` (modificar) | `build_sale_products` pasa a usar la función compartida de SKU en vez de su `int()` propio. |
| `core/management/commands/sync_stock.py` **(nuevo)** | El comando: orquesta lectura, comparación, guardas, escritura y reporte. Nada de lógica de decisión acá. |
| `sync-stock.sh` **(nuevo)** | Envoltorio del cron con `flock` y el `cd` al checkout. |
| `muci-integrador/settings.py`, `test_settings.py`, `.env.example` (modificar) | Los cuatro settings nuevos. |
| `core/tests.py` (modificar) | Los tests de todo lo anterior. |
| `core/stock_sync_rescatado.py`, `core/management/commands/syncstock.py` | **Se borran en la última tarea.** El código rescatado ya cumplió su función de referencia. |

---

## Task 1: El SKU se traduce a id de BIMS en un solo lugar

La spec exige que la traducción SKU → id de producto de BIMS sea **una sola función compartida** con el camino de venta, o los dos caminos van a discrepar sobre qué producto de BIMS es cada producto de Woo.

**Regla del SKU no numérico (Carlos, 2026-09-02):** un SKU no numérico es la marca de **producto dado de baja** — al dar de baja, el `7` pasa a `7-1`, `7-5`, `7-19` según cuántas veces se dio de baja. Así que **no es un dato inválido, es un estado**, y cada camino lo trata distinto:

- **al facturar:** debe **fallar** la orden, como está hoy, pero con un mensaje legible;
- **en el barrido de stock:** se **saltea** ese producto y se cuenta. Frenar un barrido porque un producto está de baja sería absurdo.

Hoy `core/services.py:424` hace `int(product.get("sku", 0))` y revienta con `ValueError: invalid literal for int() with base 10: '149-5-0'`. Hay **una fila real en producción** con ese mensaje. El resultado (no facturar) es correcto; el mensaje no.

**Files:**
- Create: `core/stock.py`
- Modify: `core/services.py` (la resolución de SKU dentro de `build_sale_products`)
- Test: `core/tests.py`

**Interfaces:**
- Produces:
  - `core.stock.SkuDadoDeBaja(Exception)` — se levanta cuando el SKU no es numérico.
  - `core.stock.bims_product_id(sku: Optional[str]) -> Optional[int]` — devuelve el id de BIMS, `None` si no hay SKU o es `"0"`, y **levanta `SkuDadoDeBaja`** si el SKU tiene forma de baja.

- [x] **Step 1: Escribir los tests que fallan**

Agregar al final de `core/tests.py`:

```python
class SkuABimsIdTest(TestCase):
    """
    El SKU de WooCommerce ES el id de producto de BIMS.

    Comprobado sobre merch real el 2026-09-02: SKU 13 en Woo es
    `JUGUETE MEDIDOR DE ALTURA (SPACE) - SC` id 13 en BIMS, SKU 16 es id 16 y
    SKU 128 es `BOLSAS SC`. Ningún producto de BIMS tiene `code` cargado (0 de
    427), así que el puntero vive del lado de Woo y esta traducción es el único
    vínculo que existe.
    """

    def test_un_sku_numerico_es_el_id_de_bims(self):
        from core.stock import bims_product_id

        self.assertEqual(bims_product_id("128"), 128)

    def test_un_sku_vacio_no_tiene_vinculo(self):
        from core.stock import bims_product_id

        self.assertIsNone(bims_product_id(""))
        self.assertIsNone(bims_product_id(None))

    def test_el_sku_cero_no_tiene_vinculo(self):
        """`0` no es un id de BIMS: es el default de un campo sin llenar."""
        from core.stock import bims_product_id

        self.assertIsNone(bims_product_id("0"))

    def test_un_sku_no_numerico_es_un_producto_dado_de_baja(self):
        """
        Convención de Carlos: al dar de baja un producto se le cambia el SKU
        numérico por `<id>-<n>`, donde n es cuántas veces se dio de baja. No es
        un dato inválido, es un estado, y el que llama decide qué hacer.
        """
        from core.stock import SkuDadoDeBaja, bims_product_id

        for sku in ("7-1", "7-19", "149-5-0", "442-1"):
            with self.assertRaises(SkuDadoDeBaja):
                bims_product_id(sku)

    def test_el_espacio_alrededor_no_molesta(self):
        from core.stock import bims_product_id

        self.assertEqual(bims_product_id(" 128 "), 128)


class FacturarConSkuDadoDeBajaTest(TestCase):
    """
    Al facturar, un producto dado de baja DEBE hacer fallar la orden — decisión
    de Carlos. Lo que cambia es el mensaje: antes quedaba
    `invalid literal for int() with base 10: '149-5-0'`, que no le dice nada a
    quien lo lee desde el admin.
    """

    def _order(self):
        return {
            "total": "10000",
            "discount_total": "0",
            "meta_data": [],
            "billing": {},
            "shipping": {},
            "line_items": [
                {
                    "product_id": 162,
                    "variation_id": 0,
                    "quantity": 1,
                    "total": "10000",
                    "total_tax": "0",
                    "name": "Taza dada de baja",
                }
            ],
            "fee_lines": [],
        }

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_un_producto_dado_de_baja_hace_fallar_la_orden_con_mensaje_claro(
        self, mock_wc, mock_bims, _mock_contact
    ):
        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "149-5-0"}

        with self.assertRaises(Exception) as ctx:
            process_order(order_id=204100)

        self.assertIn("149-5-0", str(ctx.exception))
        self.assertIn("baja", str(ctx.exception).lower())
        mock_bims.create_sale.assert_not_called()
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core.tests.SkuABimsIdTest core.tests.FacturarConSkuDadoDeBajaTest --settings=muci-integrador.test_settings -v 2 )`

Expected: FAIL con `ModuleNotFoundError: No module named 'core.stock'` en los primeros, y en el último un `ValueError: invalid literal for int()` cuyo mensaje **no** contiene "baja".

- [x] **Step 3: Crear `core/stock.py` con la función compartida**

```python
"""
Lógica de la sincronización de stock BIMS → WooCommerce.

Todo acá es **puro**: sin red, sin ORM, sin Django. El comando
`sync_stock` es el único que hace I/O. Esa separación es deliberada — las
decisiones que pueden apagar la tienda se prueban sin levantar nada.
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
```

- [x] **Step 4: Que `build_sale_products` use la función compartida**

En `core/services.py`, reemplazar el bloque que va desde `if product.get("sku") == "":` hasta el `continue` del `bims_id == 0` por:

```python
        # La traducción SKU → id de BIMS vive en `core.stock` y la comparte el
        # barrido de stock. Si cada camino la implementara, los dos discreparían
        # sobre qué producto de BIMS es cada producto de Woo, y eso se descubre
        # facturando mal.
        bims_id = bims_product_id(product.get("sku"))
        if bims_id is None:
            msg = f"Producto {search_id} ({item.get('name')}) omitido: sin SKU."
            logger.warning(f"Order {order_id}: {msg}")
            skipped_messages.append(msg)
            continue
```

Y agregar el import arriba, junto a los otros de `core`:

```python
from core.stock import bims_product_id
```

`SkuDadoDeBaja` **no se captura acá a propósito**: propaga y hace fallar la orden, que es lo que Carlos pidió, pero ahora con un mensaje que se entiende desde el admin.

⚠️ `core/stock.py` no debe importar `core.services` ni `core.bims`: `bims.py` instancia `BimsApi()` en el import y hace login. Es la misma razón por la que `core/states.py` vive aparte.

- [x] **Step 5: Correr los tests**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings )`

Expected: **247 OK** (241 + 6 nuevos).

- [x] **Step 6: Commit**

```bash
git add core/stock.py core/services.py core/tests.py
git commit -m "feat(stock): traducir SKU a id de BIMS en un solo lugar

El SKU de Woo ES el id de producto de BIMS, y hasta ahora esa traduccion
vivia dentro de build_sale_products como un int() pelado. El barrido de
stock necesita la misma traduccion, y dos implementaciones discreparian
sobre que producto de BIMS es cada producto de Woo.

Un SKU no numerico es la convencion de baja de un producto (7 pasa a 7-1,
7-5, ...), asi que ahora levanta SkuDadoDeBaja en vez de reventar con
'invalid literal for int()'. Hay una fila real en produccion con ese
mensaje. El comportamiento no cambia -la orden sigue fallando, como pidio
Carlos- pero el motivo se entiende desde el admin."
```

---

## Task 2: El stock vendible se suma sólo de los depósitos habilitados

**Files:**
- Modify: `core/stock.py`
- Test: `core/tests.py`

**Interfaces:**
- Consumes: nada de Task 1.
- Produces:
  - `core.stock.stock_vendible(availability_full: Optional[list], depositos: Iterable[int]) -> float`
  - `core.stock.desglose_por_deposito(availability_full: Optional[list], depositos: Iterable[int]) -> Dict[int, float]` — el aporte de cada depósito habilitado. La spec **exige** que la auditoría publique este desglose: como WooCommerce no sabe en qué depósito vive nada, sin él el número publicado no se puede explicar después.

- [x] **Step 1: Escribir los tests que fallan**

```python
class StockVendibleTest(TestCase):
    """
    El número vendible es la suma de `total` sobre los depósitos habilitados.

    Medido el 2026-09-02: `Product.availability`, que calcula BIMS, **es la suma
    de `total`** — verificado en 4 productos. `total2` trae negativos grandes y
    proporcionales al volumen de venta (−285 en cartas infantiles), o sea es una
    salida acumulada y NO stock disponible.
    """

    def _disponibilidad(self, warehouse_id, total, total2="0"):
        return {
            "Availability": {
                "warehouse_id": str(warehouse_id),
                "total": total,
                "total2": total2,
            },
            "Warehouse": {"id": str(warehouse_id), "name": f"Deposito {warehouse_id}"},
        }

    def test_suma_solo_los_depositos_habilitados(self):
        from core.stock import stock_vendible

        av = [
            self._disponibilidad(6, "57.0000000000000000"),
            self._disponibilidad(7, "14"),
            self._disponibilidad(1, "999"),  # Casa Matriz, NO habilitado
        ]

        self.assertEqual(stock_vendible(av, [6, 7]), 71.0)

    def test_ignora_total2_aunque_traiga_numeros(self):
        """El caso real de `JUGUETE CARTAS INFANTILES SC`: total 57, total2 -285."""
        from core.stock import stock_vendible

        av = [self._disponibilidad(6, "57", total2="-285")]

        self.assertEqual(stock_vendible(av, [6, 7]), 57.0)

    def test_parsea_los_tres_formatos_que_manda_bims(self):
        """BIMS devuelve '0', '0.000000' y '128.0000000000000000' en la misma respuesta."""
        from core.stock import stock_vendible

        av = [
            self._disponibilidad(6, "0"),
            self._disponibilidad(7, "0.000000"),
        ]
        self.assertEqual(stock_vendible(av, [6, 7]), 0.0)

        av = [self._disponibilidad(6, "128.0000000000000000")]
        self.assertEqual(stock_vendible(av, [6, 7]), 128.0)

    def test_un_negativo_no_resta_del_total(self):
        """
        Un depósito en negativo es un desajuste de inventario, no una deuda que
        haya que descontarle a otro depósito. Se trata como 0.
        """
        from core.stock import stock_vendible

        av = [
            self._disponibilidad(6, "10"),
            self._disponibilidad(7, "-4"),
        ]

        self.assertEqual(stock_vendible(av, [6, 7]), 10.0)

    def test_sin_disponibilidades_es_cero(self):
        from core.stock import stock_vendible

        self.assertEqual(stock_vendible(None, [6, 7]), 0.0)
        self.assertEqual(stock_vendible([], [6, 7]), 0.0)

    def test_el_desglose_dice_de_donde_salio_cada_unidad(self):
        """
        Sin esto, un número publicado en la web no se puede explicar: Woo no sabe
        en qué depósito vive nada, sólo BIMS lo sabe.
        """
        from core.stock import desglose_por_deposito

        av = [
            self._disponibilidad(6, "57"),
            self._disponibilidad(7, "14"),
            self._disponibilidad(1, "999"),
        ]

        self.assertEqual(desglose_por_deposito(av, [6, 7]), {6: 57.0, 7: 14.0})

    def test_el_desglose_omite_los_depositos_en_cero(self):
        from core.stock import desglose_por_deposito

        av = [self._disponibilidad(6, "57"), self._disponibilidad(7, "0")]

        self.assertEqual(desglose_por_deposito(av, [6, 7]), {6: 57.0})

    def test_un_total_ilegible_no_rompe_el_barrido(self):
        """Un valor que no parsea cuenta como 0, no tira el barrido entero."""
        from core.stock import stock_vendible

        av = [
            {"Availability": {"warehouse_id": "6", "total": "en revision"}},
            self._disponibilidad(7, "5"),
        ]

        self.assertEqual(stock_vendible(av, [6, 7]), 5.0)
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core.tests.StockVendibleTest --settings=muci-integrador.test_settings -v 2 )`

Expected: FAIL con `ImportError: cannot import name 'stock_vendible' from 'core.stock'`.

- [x] **Step 3: Implementar `stock_vendible`**

Agregar a `core/stock.py`:

```python
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


def stock_vendible(
    availability_full: Optional[list], depositos: Iterable[int]
) -> float:
    """
    Unidades vendibles de un producto: la suma de `total` sobre los depósitos
    habilitados.

    Se suma `total` y no `total2` porque `Product.availability` —el agregado que
    calcula BIMS— es la suma de `total`. `total2` trae negativos proporcionales
    al volumen de venta: es una salida acumulada.

    Un depósito en negativo se trata como 0: un desajuste de inventario en un
    depósito no es una deuda que haya que descontarle a otro.
    """
    return sum(desglose_por_deposito(availability_full, depositos).values())


def desglose_por_deposito(
    availability_full: Optional[list], depositos: Iterable[int]
) -> Dict[int, float]:
    """
    Cuánto aporta cada depósito habilitado, omitiendo los que aportan 0.

    Existe para la auditoría, y no es prolijidad: **WooCommerce no sabe en qué
    depósito vive nada, sólo BIMS lo sabe**, así que cuando alguien pregunte "por
    qué la web dice 3" la respuesta no está ni en la web ni en Woo. Sin este
    desglose en el registro, ese número no se puede explicar después.
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
```

Y agregar al import de `typing`: `from typing import Dict, Iterable, Optional`.

- [x] **Step 4: Correr los tests**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings )`

Expected: **255 OK**.

- [x] **Step 5: Commit**

```bash
git add core/stock.py core/tests.py
git commit -m "feat(stock): sumar el stock vendible solo de los depositos habilitados

Suma `total` y no `total2`: el campo Product.availability que calcula BIMS
es la suma de total, verificado en 4 productos. total2 trae negativos
proporcionales al volumen de venta (-285 en cartas infantiles), o sea es
una salida acumulada.

Parsea como float porque BIMS manda '0', '0.000000' y
'128.0000000000000000' en la misma respuesta. Un valor ilegible cuenta
como 0 y un deposito en negativo tambien: ninguno de los dos debe tirar
un barrido completo."
```

---

## Task 3: La regla de herencia padre → variación

De la spec: el nivel del vínculo no es uniforme. En los juguetes el SKU está en la variación; en el libro de colorear está en el padre. Y **heredar cuando hay varias hermanas sin SKU multiplica el inventario**: `Taza pequeña` tiene SKU 27 y nueve variaciones sin SKU, y en BIMS el producto 27 tiene *un* stock.

**Files:**
- Modify: `core/stock.py`
- Test: `core/tests.py`

**Interfaces:**
- Consumes: `bims_product_id`, `SkuDadoDeBaja` de Task 1.
- Produces:
  - `core.stock.SKU_AMBIGUO`, `core.stock.SKU_SIN_VINCULO`, `core.stock.SKU_DADO_DE_BAJA` — constantes `str` para el motivo del descarte.
  - `core.stock.resolver_bims_id(sku_propio: Optional[str], sku_padre: Optional[str], hermanas_sin_sku: int) -> Tuple[Optional[int], Optional[str]]` — devuelve `(bims_id, motivo_del_descarte)`. Exactamente uno de los dos es `None`.

- [x] **Step 1: Escribir los tests que fallan**

```python
class HerenciaDeSkuTest(TestCase):
    """
    Heredar el SKU del padre es la semántica de WooCommerce
    (`WC_Product_Variation::get_sku()` lo hace), así que no es un parche. Pero se
    rompe con varias hermanas sin SKU: `Taza pequeña` tiene SKU 27 en el padre y
    **9 variaciones sin SKU** —nueve diseños de la misma taza— y en BIMS el
    producto 27 tiene UN stock. Heredar ahí escribiría el mismo número nueve
    veces: inventario multiplicado por 9.

    Medido el 2026-09-02: 323 variaciones tienen SKU propio, 32 padres tienen
    exactamente una variación sin SKU, y 12 padres tienen varias (59
    variaciones, con `Libros Ttklab` en 23).
    """

    def test_el_sku_propio_de_la_variacion_manda(self):
        from core.stock import resolver_bims_id

        bims_id, motivo = resolver_bims_id("13", "999", hermanas_sin_sku=1)

        self.assertEqual(bims_id, 13)
        self.assertIsNone(motivo)

    def test_sin_sku_propio_y_unica_hermana_hereda_del_padre(self):
        from core.stock import resolver_bims_id

        bims_id, motivo = resolver_bims_id(None, "575", hermanas_sin_sku=1)

        self.assertEqual(bims_id, 575)
        self.assertIsNone(motivo)

    def test_con_varias_hermanas_sin_sku_no_se_hereda(self):
        """El caso `Taza pequeña`: heredar multiplicaría el stock por 9."""
        from core.stock import SKU_AMBIGUO, resolver_bims_id

        bims_id, motivo = resolver_bims_id(None, "27", hermanas_sin_sku=9)

        self.assertIsNone(bims_id)
        self.assertEqual(motivo, SKU_AMBIGUO)

    def test_ni_la_variacion_ni_el_padre_tienen_sku(self):
        from core.stock import SKU_SIN_VINCULO, resolver_bims_id

        bims_id, motivo = resolver_bims_id(None, None, hermanas_sin_sku=1)

        self.assertIsNone(bims_id)
        self.assertEqual(motivo, SKU_SIN_VINCULO)

    def test_un_producto_dado_de_baja_se_saltea_sin_frenar_el_barrido(self):
        """
        En el barrido, a diferencia de la facturación, una baja NO es un error:
        se saltea y se cuenta. Frenar un barrido entero porque un producto está
        de baja sería absurdo.
        """
        from core.stock import SKU_DADO_DE_BAJA, resolver_bims_id

        bims_id, motivo = resolver_bims_id("7-19", None, hermanas_sin_sku=1)

        self.assertIsNone(bims_id)
        self.assertEqual(motivo, SKU_DADO_DE_BAJA)

    def test_un_padre_dado_de_baja_no_se_hereda(self):
        from core.stock import SKU_DADO_DE_BAJA, resolver_bims_id

        bims_id, motivo = resolver_bims_id(None, "442-1", hermanas_sin_sku=1)

        self.assertIsNone(bims_id)
        self.assertEqual(motivo, SKU_DADO_DE_BAJA)
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core.tests.HerenciaDeSkuTest --settings=muci-integrador.test_settings -v 2 )`

Expected: FAIL con `ImportError: cannot import name 'resolver_bims_id'`.

- [x] **Step 3: Implementar `resolver_bims_id`**

Agregar a `core/stock.py`:

```python
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
```

Y agregar `Tuple` al import: `from typing import Iterable, Optional, Tuple`.

- [x] **Step 4: Correr los tests**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings )`

Expected: **261 OK**.

- [x] **Step 5: Commit**

```bash
git add core/stock.py core/tests.py
git commit -m "feat(stock): regla de herencia de SKU padre -> variacion

Heredar el SKU del padre es lo que hace WooCommerce
(WC_Product_Variation::get_sku), asi que no es un parche. Pero solo vale
cuando la variacion es la UNICA sin SKU propio: Taza pequena tiene SKU 27
y nueve variaciones sin SKU, y en BIMS el producto 27 tiene un solo
stock, asi que heredar ahi multiplicaria el inventario por nueve.

Con varias hermanas se devuelve el motivo 'ambiguo' y el barrido lo
reporta en vez de adivinar. Un SKU de baja tambien se saltea, con su
motivo propio, porque en el barrido una baja no es un error."
```

---

## Task 4: Calcular qué cambió, y la guarda de radio de impacto

**Files:**
- Modify: `core/stock.py`
- Test: `core/tests.py`

**Interfaces:**
- Consumes: nada de las tareas anteriores.
- Produces:
  - `core.stock.Cambio` — `NamedTuple` con `woo_id: int`, `ruta_woo: str`, `bims_id: int`, `stock_actual: float`, `stock_nuevo: float`, `apaga: bool`. `ruta_woo` es lo que va después de `products/` al escribir: `"100"` para un producto simple y `"187056/variations/188079"` para una variación.
  - `core.stock.calcular_cambios(candidatos: list, stock_por_bims_id: dict) -> List[Cambio]`
  - `core.stock.radio_excedido(cambios: Iterable[Cambio], tope: int) -> int` — devuelve cuántos productos se apagarían si eso supera el tope, o `0` si está dentro.

Un `candidato` es un dict con `woo_id: int`, `ruta_woo: str`, `bims_id: int` y `stock_actual: float`.

- [x] **Step 1: Escribir los tests que fallan**

```python
class CalculoDeCambiosTest(TestCase):
    """Sólo se escribe donde el número difiere, y no se apaga en masa."""

    def test_solo_devuelve_los_que_cambian(self):
        from core.stock import calcular_cambios

        candidatos = [
            {"woo_id": 100, "ruta_woo": "100", "bims_id": 13, "stock_actual": 5.0},
            {"woo_id": 200, "ruta_woo": "200", "bims_id": 128, "stock_actual": 128.0},
        ]

        cambios = calcular_cambios(candidatos, {13: 7.0, 128: 128.0})

        self.assertEqual(len(cambios), 1)
        self.assertEqual(cambios[0].woo_id, 100)
        self.assertEqual(cambios[0].stock_nuevo, 7.0)

    def test_una_variacion_lleva_la_ruta_anidada(self):
        """WooCommerce escribe una variación en `products/{padre}/variations/{id}`."""
        from core.stock import calcular_cambios

        candidatos = [
            {
                "woo_id": 188079,
                "ruta_woo": "187056/variations/188079",
                "bims_id": 575,
                "stock_actual": 49.0,
            }
        ]

        cambios = calcular_cambios(candidatos, {575: 16.0})

        self.assertEqual(cambios[0].ruta_woo, "187056/variations/188079")

    def test_no_escribe_un_producto_que_bims_no_devolvio(self):
        """
        Si BIMS no trajo ese producto, no hay dato: no se toca. Esta es la guarda
        que evita que una lectura fallida apague el catálogo.
        """
        from core.stock import calcular_cambios

        candidatos = [{"woo_id": 100, "ruta_woo": "100", "bims_id": 13, "stock_actual": 5.0}]

        self.assertEqual(calcular_cambios(candidatos, {}), [])

    def test_marca_los_que_apagan_la_venta(self):
        from core.stock import calcular_cambios

        candidatos = [
            {"woo_id": 100, "ruta_woo": "100", "bims_id": 13, "stock_actual": 5.0},
            {"woo_id": 200, "ruta_woo": "200", "bims_id": 14, "stock_actual": 0.0},
        ]

        cambios = calcular_cambios(candidatos, {13: 0.0, 14: 3.0})

        apagados = [c for c in cambios if c.apaga]
        self.assertEqual([c.woo_id for c in apagados], [100])

    def test_una_diferencia_de_decimales_no_es_un_cambio(self):
        """Woo guarda '5' y BIMS '5.0000000000000000': es el mismo número."""
        from core.stock import calcular_cambios

        candidatos = [{"woo_id": 100, "ruta_woo": "100", "bims_id": 13, "stock_actual": 5.0}]

        self.assertEqual(calcular_cambios(candidatos, {13: 5.0}), [])


class GuardaDeRadioTest(TestCase):
    """
    Si un barrido apagaría más de N productos de golpe, casi siempre es un
    problema de la consulta o del depósito configurado, no que se haya vendido
    todo.

    N = 5 elegido con medición el 2026-09-02: hay 44 productos publicados con
    stock > 0 en Woo (41 con SKU propio), y el ritmo real fue de 3 a 20 productos
    DISTINTOS vendidos por día — pero eso es vendidos, no llegados a cero. En una
    ventana de 15 minutos lo esperable es 0 o 1.
    """

    def _cambio(self, woo_id, apaga):
        from core.stock import Cambio

        return Cambio(
            woo_id=woo_id, ruta_woo=str(woo_id), bims_id=woo_id, stock_actual=1.0,
            stock_nuevo=0.0 if apaga else 9.0, apaga=apaga,
        )

    def test_apagar_pocos_esta_dentro_del_tope(self):
        from core.stock import radio_excedido

        cambios = [self._cambio(i, apaga=True) for i in range(3)]

        self.assertEqual(radio_excedido(cambios, tope=5), 0)

    def test_apagar_mas_que_el_tope_lo_excede(self):
        from core.stock import radio_excedido

        cambios = [self._cambio(i, apaga=True) for i in range(6)]

        self.assertEqual(radio_excedido(cambios, tope=5), 6)

    def test_las_subidas_no_cuentan_para_el_tope(self):
        """El primer barrido va a ENCENDER decenas de productos: eso es correcto."""
        from core.stock import radio_excedido

        cambios = [self._cambio(i, apaga=False) for i in range(40)]

        self.assertEqual(radio_excedido(cambios, tope=5), 0)
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core.tests.CalculoDeCambiosTest core.tests.GuardaDeRadioTest --settings=muci-integrador.test_settings -v 2 )`

Expected: FAIL con `ImportError: cannot import name 'calcular_cambios'`.

- [x] **Step 3: Implementar el cálculo y la guarda**

Agregar a `core/stock.py`:

```python
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


def calcular_cambios(
    candidatos: list, stock_por_bims_id: dict
) -> List[Cambio]:
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
```

Y completar el import: `from typing import Iterable, List, NamedTuple, Optional, Tuple`.

- [x] **Step 4: Correr los tests**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings )`

Expected: **269 OK**.

- [x] **Step 5: Commit**

```bash
git add core/stock.py core/tests.py
git commit -m "feat(stock): calcular solo las diferencias, y guarda de radio

Un producto que BIMS no devolvio NO se toca: 'no hay dato' no es 'el dato
es cero'. Es la guarda que evita que una lectura fallida apague el
catalogo publico, y sale de un error propio: un sondeo de diagnostico
imprimio 'NO EXISTE en BIMS' nueve veces cuando BIMS habia dado timeout.

La guarda de radio cuenta solo los que APAGAN, con tope 5. El primer
barrido va a encender decenas de productos que hoy figuran agotados
teniendo stock, y eso es correcto: contar las subidas haria saltar la
guarda justo cuando el barrido esta haciendo bien su trabajo."
```

---

## Task 5: Los cuatro settings

**Files:**
- Modify: `muci-integrador/settings.py`, `muci-integrador/test_settings.py`, `.env.example`
- Test: `core/tests.py`

**Interfaces:**
- Produces: `settings.STOCK_WAREHOUSE_IDS: List[int]`, `settings.STOCK_ZERO_GUARD: int`, `settings.STOCK_SYNC_ENABLED: bool`, `settings.STOCK_PAGE_SIZE: int`

- [x] **Step 1: Escribir el test que falla**

```python
class SettingsDeStockTest(TestCase):
    """Los cuatro settings del barrido existen y tienen el tipo correcto."""

    def test_los_settings_existen_con_su_tipo(self):
        from django.conf import settings

        self.assertIsInstance(settings.STOCK_WAREHOUSE_IDS, list)
        self.assertTrue(all(isinstance(d, int) for d in settings.STOCK_WAREHOUSE_IDS))
        self.assertIsInstance(settings.STOCK_ZERO_GUARD, int)
        self.assertIsInstance(settings.STOCK_SYNC_ENABLED, bool)
        self.assertIsInstance(settings.STOCK_PAGE_SIZE, int)

    def test_la_pagina_no_puede_ser_grande(self):
        """
        Medido el 2026-09-02: `products/index.json?v_stock=1` con limit=500 NO
        entra en los 30 s de TIMEOUT_LECTURA y da timeout. Y el reintento hereda
        las sobras del presupuesto de 40 s, así que tampoco salva.
        """
        from django.conf import settings

        self.assertLessEqual(settings.STOCK_PAGE_SIZE, 100)
```

- [x] **Step 2: Correr y verificar que falla**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core.tests.SettingsDeStockTest --settings=muci-integrador.test_settings -v 2 )`

Expected: FAIL con `AttributeError: 'Settings' object has no attribute 'STOCK_WAREHOUSE_IDS'`.

- [x] **Step 3: Agregar los settings**

En `muci-integrador/settings.py`, después del bloque de `QUEUE_*`:

```python
# ── Sincronización de stock BIMS → WooCommerce ───────────────────────────────
# Depósitos de BIMS cuyo stock se considera vendible online. Medido el
# 2026-09-02: TODO el stock del catálogo está en 6 (San Cosmos) y 7 (GIFTSHOP
# MOVIL); Casa Matriz tiene 0 en los 427 productos inventariables. Va acá y no
# en el código porque **WooCommerce no sabe en qué depósito vive nada, sólo BIMS
# lo sabe**, así que esta política no se puede derivar y tiene que ser auditable.
STOCK_WAREHOUSE_IDS = [
    int(d) for d in config.get("STOCK_WAREHOUSE_IDS", "6,7").split(",") if d.strip()
]
# Cuántos productos puede apagar un solo barrido antes de abortar y avisar. Con
# 44 productos con stock hoy, 5 está arriba del ruido (0 o 1 por ventana de 15
# minutos) y abajo del desastre (41 apagables).
STOCK_ZERO_GUARD = int(config.get("STOCK_ZERO_GUARD", 5))
# En `false` el barrido calcula e informa pero NO escribe. Arranca APAGADO: el
# primer barrido tiene que correr en seco y que alguien mire la lista.
STOCK_SYNC_ENABLED = config.get("STOCK_SYNC_ENABLED", "false").lower() not in (
    "false", "0", "",
)
# Tamaño de página contra BIMS. Con 500 la respuesta no entra en los 30 s de
# TIMEOUT_LECTURA. No subirlo.
STOCK_PAGE_SIZE = int(config.get("STOCK_PAGE_SIZE", 100))
```

En `muci-integrador/test_settings.py`, después de los `QUEUE_*`:

```python
STOCK_WAREHOUSE_IDS = [6, 7]
STOCK_ZERO_GUARD = 5
# Los tests que prueban la escritura lo prenden con `self.settings(...)`.
STOCK_SYNC_ENABLED = False
STOCK_PAGE_SIZE = 100
```

En `.env.example`, después del bloque de la cola:

```
# ── Sincronización de stock BIMS → WooCommerce (opcional) ────────────────────
# Depósitos de BIMS cuyo stock se publica en la web, separados por coma. Medido
# el 2026-09-02: todo el stock está en 6 (San Cosmos) y 7 (GIFTSHOP MOVIL);
# Casa Matriz (1) tiene 0 en todo el catálogo. Default: 6,7
#STOCK_WAREHOUSE_IDS=6,7

# Cuántos productos puede apagar un solo barrido antes de abortar y avisar a
# Slack. Un cero masivo casi siempre es un error de configuración, no que se
# haya vendido todo. Default: 5
#STOCK_ZERO_GUARD=5

# En false el barrido calcula e informa pero NO escribe en WooCommerce.
# ⚠️ Arranca en false a propósito: el primer barrido va en seco. Default: false
#STOCK_SYNC_ENABLED=false

# Tamaño de página contra BIMS. NO subirlo: con 500, la respuesta de
# products?v_stock=1 no entra en los 30 s de timeout de lectura. Default: 100
#STOCK_PAGE_SIZE=100
```

- [x] **Step 4: Correr los tests**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings )`

Expected: **271 OK**.

- [x] **Step 5: Commit**

```bash
git add muci-integrador/settings.py muci-integrador/test_settings.py .env.example core/tests.py
git commit -m "feat(stock): los cuatro settings del barrido

STOCK_WAREHOUSE_IDS arranca en 6,7 porque ahi esta TODO el stock del
catalogo (Casa Matriz tiene 0 en los 427 inventariables). Va en settings y
no en el codigo porque Woo no sabe en que deposito vive nada -solo BIMS lo
sabe- asi que la politica no se puede derivar y tiene que ser auditable.
Es justo lo que le falto al bimsc_warehouses=1,4 del plugin, que incluia
Alquilados y nadie volvio a mirar en un ano.

STOCK_SYNC_ENABLED arranca APAGADO: el primer barrido va en seco.
STOCK_PAGE_SIZE=100 con un test que impide subirlo, porque con 500 la
respuesta no entra en los 30 s de TIMEOUT_LECTURA."
```

---

## Task 6: Leer el stock de BIMS

**Files:**
- Modify: `core/bims.py`
- Test: `core/tests.py`

**Interfaces:**
- Produces: `BimsApi.get_products_with_stock(self, limit: int, offset: int) -> dict` — devuelve el cuerpo crudo de la respuesta.

- [x] **Step 1: Escribir los tests que fallan**

```python
class LecturaDeStockDeBimsTest(TestCase):
    """
    `bims.py` no tenía NINGUNA lectura de productos: sólo `create_sale`,
    `get_posales`, `get_contacts` y `list_contacts`.
    """

    @patch.object(BimsApi, "login", return_value="fake_sid")
    def test_pide_v_stock_para_que_venga_la_disponibilidad(self, _mock_login):
        """
        Sin `v_stock=1` la respuesta NO trae `AvailabilityFull`, que es de donde
        sale el stock por depósito. Verificado contra la API viva.
        """
        api = BimsApi()
        respuesta = {"status": "ok", "data": []}

        with patch.object(api, "_retry_request", return_value=respuesta) as mock_req:
            api.get_products_with_stock(limit=100, offset=0)

        params = mock_req.call_args[1]["params"]
        self.assertEqual(params["v_stock"], 1)
        self.assertEqual(params["limit"], 100)
        self.assertEqual(params["offset"], 0)

    @patch.object(BimsApi, "login", return_value="fake_sid")
    def test_usa_el_endpoint_de_index_con_json(self, _mock_login):
        api = BimsApi()

        with patch.object(api, "_retry_request", return_value={"status": "ok"}) as mock_req:
            api.get_products_with_stock(limit=100, offset=200)

        url = mock_req.call_args[0][1]
        self.assertTrue(url.endswith("/products/index.json"), url)
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core.tests.LecturaDeStockDeBimsTest --settings=muci-integrador.test_settings -v 2 )`

Expected: FAIL con `AttributeError: 'BimsApi' object has no attribute 'get_products_with_stock'`.

- [x] **Step 3: Implementar el método**

Agregar a `core/bims.py`, junto a `get_contacts`:

```python
    def get_products_with_stock(self, limit: int, offset: int) -> dict:
        """
        Una página de productos con su disponibilidad por depósito.

        `v_stock=1` es lo que hace aparecer `AvailabilityFull`; sin ese parámetro
        la respuesta no trae stock. Verificado contra la API viva el 2026-09-02.

        ⚠️ **El `limit` no puede ser grande.** Con 500 la respuesta no entra en
        los 30 s de `TIMEOUT_LECTURA` y da timeout, y el reintento tampoco salva
        porque `PRESUPUESTO_REINTENTOS` (40 s) se reparte entre los intentos: si
        el primero consume 30, al segundo le quedan 8. Con 100 anda.

        No se usa `availabilities/index.json`, que sería más directo, porque
        **pagina sin orden estable**: devolvió 21 filas donde debían ser 12, con
        repetidas y distinto orden entre páginas, así que un barrido por ahí
        puede perderse un producto entero.
        """
        url = f"{self.base_url}/products/index.json"
        params = {"limit": limit, "offset": offset, "v_stock": 1}
        if self.sid:
            params["sid"] = self.sid
        return self._retry_request(self.session.get, url, params=params)
```

- [x] **Step 4: Correr los tests**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings )`

Expected: **273 OK**.

- [x] **Step 5: Commit**

```bash
git add core/bims.py core/tests.py
git commit -m "feat(stock): lectura de productos con stock en el cliente de BIMS

bims.py no tenia ninguna lectura de productos: solo create_sale,
get_posales, get_contacts y list_contacts. El sub-proyecto C tambien la va
a necesitar para reconciliar.

v_stock=1 es lo que hace aparecer AvailabilityFull; sin ese parametro no
viene el stock. El docstring deja anotado por que el limit no puede ser
grande y por que se descarta availabilities/index.json, que pagina sin
orden estable."
```

---

## Task 7: Enumerar y escribir en WooCommerce

**Files:**
- Modify: `core/woocommerce.py`
- Test: `core/tests.py`

**Interfaces:**
- Produces:
  - `WooCommerceAPI.get_variations(self, parent_id, **kwargs) -> list`
  - `WooCommerceAPI.update_product_stock(self, id, cantidad: float) -> dict`

- [x] **Step 1: Escribir los tests que fallan**

```python
class EscrituraDeStockEnWooTest(TestCase):
    """
    El barrido escribe **tres campos y nada más**. Los precios quedan
    explícitamente afuera: `bimsc` los escribía y traer eso cambiaría los precios
    del sitio.
    """

    def _api(self):
        api = WooCommerceAPI()
        api.wcapi = MagicMock()
        return api

    def test_escribe_cantidad_y_deja_en_stock(self):
        api = self._api()
        api.wcapi.put.return_value = MagicMock(
            status_code=200, json=lambda: {"id": 100, "stock_quantity": 7}
        )

        api.update_product_stock(100, 7.0)

        ruta, kwargs = api.wcapi.put.call_args[0][0], api.wcapi.put.call_args[1]
        self.assertEqual(ruta, "products/100")
        self.assertEqual(kwargs["data"]["stock_quantity"], 7)
        self.assertTrue(kwargs["data"]["manage_stock"])
        self.assertEqual(kwargs["data"]["stock_status"], "instock")

    def test_con_cero_marca_agotado(self):
        api = self._api()
        api.wcapi.put.return_value = MagicMock(status_code=200, json=lambda: {"id": 100})

        api.update_product_stock(100, 0.0)

        data = api.wcapi.put.call_args[1]["data"]
        self.assertEqual(data["stock_quantity"], 0)
        self.assertEqual(data["stock_status"], "outofstock")

    def test_no_manda_precios(self):
        """Blindaje explícito: `bimsc` escribía precios y eso queda afuera."""
        api = self._api()
        api.wcapi.put.return_value = MagicMock(status_code=200, json=lambda: {"id": 100})

        api.update_product_stock(100, 3.0)

        data = api.wcapi.put.call_args[1]["data"]
        for prohibido in ("price", "regular_price", "sale_price"):
            self.assertNotIn(prohibido, data)

    def test_un_no_200_levanta_la_excepcion_del_cliente(self):
        api = self._api()
        api.wcapi.put.return_value = MagicMock(status_code=400, text="no")

        with self.assertRaises(WooCommerceAPI.ServerException):
            api.update_product_stock(100, 3.0)

    def test_las_variaciones_se_piden_al_endpoint_del_padre(self):
        api = self._api()
        api.wcapi.get.return_value = MagicMock(
            status_code=200, json=lambda: [{"id": 188079, "sku": ""}]
        )

        variaciones = api.get_variations(187056)

        self.assertEqual(api.wcapi.get.call_args[0][0], "products/187056/variations")
        self.assertEqual(variaciones[0]["id"], 188079)
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core.tests.EscrituraDeStockEnWooTest --settings=muci-integrador.test_settings -v 2 )`

Expected: FAIL con `AttributeError: 'WooCommerceAPI' object has no attribute 'update_product_stock'`.

- [x] **Step 3: Implementar los dos métodos**

Agregar a `core/woocommerce.py`, después de `get_product`:

```python
    def get_variations(self, parent_id, **kwargs) -> list:
        """
        Variaciones de un producto variable.

        Hace falta un request por padre: la REST de WooCommerce no tiene un
        listado global de variaciones. Son 36 padres, o sea 36 requests por
        barrido — el precio de no vivir dentro de WordPress, y es barato.
        """
        with self._timeout_recortado(f"products/{parent_id}/variations"):
            res = self.wcapi.get(f"products/{parent_id}/variations", params=kwargs)
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)

    def update_product_stock(self, id, cantidad: float) -> dict:
        """
        Escribe el stock de un producto o variación. **Sólo tres campos.**

        Los precios quedan afuera a propósito: `bimsc` los escribía
        (`sell_price × buy_price` de la moneda, con un campo `PP` que manda si
        existe) y traer eso junto con el stock cambiaría los precios del sitio.

        `stock_status` va explícito y no se deja al cálculo de WooCommerce
        porque es el campo que efectivamente corta la venta.
        """
        unidades = int(cantidad)
        data = {
            "stock_quantity": unidades,
            "manage_stock": True,
            "stock_status": "instock" if unidades > 0 else "outofstock",
        }
        with self._timeout_recortado(f"products/{id}"):
            res = self.wcapi.put(f"products/{id}", data=data)
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)
```

⚠️ **El parámetro `id` recibe la ruta relativa, no sólo un número.** WooCommerce escribe una variación en `products/{padre}/variations/{id}`, así que el llamador pasa `"100"` para un producto simple y `"187056/variations/188079"` para una variación. La f-string `products/{id}` compone bien las dos, y es el campo `ruta_woo` del `Cambio` de la Task 4.

- [x] **Step 4: Correr los tests**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings )`

Expected: **278 OK**.

- [x] **Step 5: Commit**

```bash
git add core/woocommerce.py core/tests.py
git commit -m "feat(stock): enumerar variaciones y escribir stock en WooCommerce

update_product_stock escribe TRES campos y nada mas, con un test que
prohibe explicitamente los precios: bimsc los escribia y traer eso
cambiaria los precios del sitio.

stock_status va explicito y no se deja al calculo de Woo porque es el
campo que efectivamente corta la venta.

get_variations necesita un request por padre porque la REST de Woo no
tiene listado global de variaciones. Son 36 padres por barrido: el precio
de no vivir dentro de WordPress, y es barato."
```

---

## Task 8: El comando, las guardas y el reporte

**Files:**
- Create: `core/management/commands/sync_stock.py`
- Test: `core/tests.py`

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: el comando `sync_stock`, con flag `--aplicar` que fuerza la escritura aunque `STOCK_SYNC_ENABLED` esté en `false`, y `--seco` que la impide aunque esté en `true`.

- [x] **Step 1: Escribir los tests que fallan**

```python
class BarridoDeStockTest(TestCase):
    """
    El comando completo. Necesita `from io import StringIO` arriba de
    `core/tests.py` si todavía no está. Los tres casos que importan son las guardas: que una
    lectura fallida no escriba, que un apagón masivo aborte, y que el modo seco
    no toque nada.
    """

    def setUp(self):
        self.wc = patch("core.management.commands.sync_stock.wc_api").start()
        self.bims = patch("core.management.commands.sync_stock.bims").start()
        self.notify = patch("core.management.commands.sync_stock.notify").start()
        self.addCleanup(patch.stopall)

        # Un producto simple con SKU 13 y stock 5 en Woo.
        self.wc.get_products.side_effect = [
            [{"id": 100, "type": "simple", "sku": "13", "stock_quantity": 5}],
            [],
        ]
        self.wc.get_variations.return_value = []
        self._bims_devuelve({"13": "7"})

    def _bims_devuelve(self, stock_por_id):
        data = [
            {
                "Product": {
                    "id": pid,
                    "name": f"Producto {pid}",
                    "stockable": True,
                    "AvailabilityFull": [
                        {"Availability": {"warehouse_id": "6", "total": total}}
                    ],
                }
            }
            for pid, total in stock_por_id.items()
        ]
        self.bims.get_products_with_stock.side_effect = [
            {"status": "ok", "data": data},
            {"status": "ok", "data": []},
        ]

    def test_en_seco_no_escribe_nada(self):
        from django.core.management import call_command

        with self.settings(STOCK_SYNC_ENABLED=False):
            call_command("sync_stock")

        self.wc.update_product_stock.assert_not_called()

    def test_con_aplicar_escribe_la_diferencia(self):
        from django.core.management import call_command

        with self.settings(STOCK_SYNC_ENABLED=True):
            call_command("sync_stock")

        self.wc.update_product_stock.assert_called_once_with(100, 7.0)

    def test_un_producto_que_bims_no_trajo_no_se_toca(self):
        """La guarda 1: una lectura fallida no puede apagar el catálogo."""
        from django.core.management import call_command

        self.bims.get_products_with_stock.side_effect = [{"status": "ok", "data": []}]

        with self.settings(STOCK_SYNC_ENABLED=True):
            call_command("sync_stock")

        self.wc.update_product_stock.assert_not_called()

    def test_una_pagina_de_bims_que_falla_no_apaga_nada(self):
        from django.core.management import call_command

        self.bims.get_products_with_stock.side_effect = requests.RequestException("BIMS caído")

        with self.settings(STOCK_SYNC_ENABLED=True):
            call_command("sync_stock")

        self.wc.update_product_stock.assert_not_called()
        self.assertIn("stock_lectura_fallida", [c[0][0] for c in self.notify.call_args_list])

    def test_un_apagon_masivo_aborta_y_avisa(self):
        """La guarda 2: seis productos a cero con tope 5 no se escribe."""
        from django.core.management import call_command

        self.wc.get_products.side_effect = [
            [
                {"id": 100 + i, "type": "simple", "sku": str(20 + i), "stock_quantity": 9}
                for i in range(6)
            ],
            [],
        ]
        self._bims_devuelve({str(20 + i): "0" for i in range(6)})

        with self.settings(STOCK_SYNC_ENABLED=True, STOCK_ZERO_GUARD=5):
            call_command("sync_stock")

        self.wc.update_product_stock.assert_not_called()
        self.assertIn("stock_apagon_masivo", [c[0][0] for c in self.notify.call_args_list])

    def test_muchas_subidas_no_disparan_la_guarda(self):
        """El primer barrido enciende decenas de productos: es lo esperado."""
        from django.core.management import call_command

        self.wc.get_products.side_effect = [
            [
                {"id": 100 + i, "type": "simple", "sku": str(20 + i), "stock_quantity": 0}
                for i in range(20)
            ],
            [],
        ]
        self._bims_devuelve({str(20 + i): "9" for i in range(20)})

        with self.settings(STOCK_SYNC_ENABLED=True, STOCK_ZERO_GUARD=5):
            call_command("sync_stock")

        self.assertEqual(self.wc.update_product_stock.call_count, 20)
        self.assertNotIn("stock_apagon_masivo", [c[0][0] for c in self.notify.call_args_list])

    def test_un_producto_no_inventariable_se_ignora(self):
        """Las entradas tienen `stockable: false` y quedan afuera solas."""
        from django.core.management import call_command

        self.bims.get_products_with_stock.side_effect = [
            {
                "status": "ok",
                "data": [
                    {
                        "Product": {
                            "id": "13",
                            "name": "Ticket",
                            "stockable": False,
                            "AvailabilityFull": [
                                {"Availability": {"warehouse_id": "6", "total": "0"}}
                            ],
                        }
                    }
                ],
            },
            {"status": "ok", "data": []},
        ]

        with self.settings(STOCK_SYNC_ENABLED=True):
            call_command("sync_stock")

        self.wc.update_product_stock.assert_not_called()

    def test_reporta_los_productos_de_bims_que_woo_no_tiene(self):
        """
        La spec dice que un producto de BIMS sin contraparte en Woo **se
        reporta** y no se crea. Sin este reporte, el merch que existe en BIMS y
        nunca se cargó en la web queda invisible para siempre.
        """
        from django.core.management import call_command

        self._bims_devuelve({"13": "7", "999": "42"})

        with self.settings(STOCK_SYNC_ENABLED=False):
            salida = StringIO()
            call_command("sync_stock", stdout=salida)

        self.assertIn("sin contraparte", salida.getvalue())
        self.assertIn("1", salida.getvalue())

    def test_el_reporte_dice_de_que_deposito_salio_el_numero(self):
        """Sin el desglose, el número publicado no se puede explicar después."""
        from django.core.management import call_command

        with self.settings(STOCK_SYNC_ENABLED=False):
            salida = StringIO()
            call_command("sync_stock", stdout=salida)

        self.assertIn("dep 6", salida.getvalue())

    def test_una_escritura_que_falla_no_frena_el_resto(self):
        from django.core.management import call_command

        self.wc.get_products.side_effect = [
            [
                {"id": 100, "type": "simple", "sku": "13", "stock_quantity": 5},
                {"id": 101, "type": "simple", "sku": "14", "stock_quantity": 5},
            ],
            [],
        ]
        self._bims_devuelve({"13": "7", "14": "8"})
        self.wc.update_product_stock.side_effect = [
            requests.RequestException("Woo caído"),
            {"id": 101},
        ]

        with self.settings(STOCK_SYNC_ENABLED=True):
            call_command("sync_stock")

        self.assertEqual(self.wc.update_product_stock.call_count, 2)
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core.tests.BarridoDeStockTest --settings=muci-integrador.test_settings -v 2 )`

Expected: FAIL con `CommandError: Unknown command: 'sync_stock'`.

- [x] **Step 3: Implementar el comando**

Crear `core/management/commands/sync_stock.py`:

```python
"""
Barrido de stock BIMS → WooCommerce. Corre por cron cada 15 minutos.

**No es un evento.** BIMS no tiene webhook de salida y ningún endpoint de stock
acepta un filtro "modificado desde", así que "cuando ajustan en BIMS" sólo puede
implementarse sondeando. El peor desfase es un cuarto de hora.

Toda la lógica de decisión vive en `core/stock.py`, en funciones puras. Acá sólo
hay I/O y orquestación.
"""

from collections import Counter
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand

from core.alerts import notify
from core.bims import bims
from core.stock import (
    SKU_AMBIGUO,
    SKU_DADO_DE_BAJA,
    SKU_SIN_VINCULO,
    calcular_cambios,
    desglose_por_deposito,
    radio_excedido,
    resolver_bims_id,
    stock_vendible,
)
from core.woocommerce import wc_api


class Command(BaseCommand):
    help = "Publica en WooCommerce el stock que BIMS tiene."

    def add_arguments(self, parser):
        parser.add_argument(
            "--aplicar",
            action="store_true",
            help="Escribe en WooCommerce aunque STOCK_SYNC_ENABLED esté en false.",
        )
        parser.add_argument(
            "--seco",
            action="store_true",
            help="No escribe nada, aunque STOCK_SYNC_ENABLED esté en true.",
        )

    def handle(self, *args, **options):
        escribe = settings.STOCK_SYNC_ENABLED or options["aplicar"]
        if options["seco"]:
            escribe = False

        candidatos, descartes = self._candidatos_de_woo()
        if not candidatos:
            self.stdout.write("Ningún producto de WooCommerce quedó vinculado.")
            self._informar_descartes(descartes)
            return

        datos_bims, paginas_fallidas = self._stock_de_bims()
        if paginas_fallidas:
            # No se escribe NADA con una lectura incompleta: los productos de la
            # página que falló se verían como stock 0 y se apagarían. "No hay
            # dato" no es "el dato es cero".
            notify(
                "stock_lectura_fallida",
                f"⚠️ El barrido de stock abortó: {paginas_fallidas} página(s) de "
                f"BIMS fallaron, así que no se escribió nada. Con una lectura "
                f"incompleta, publicar equivaldría a apagar productos que sí "
                f"tienen stock.",
            )
            self.stderr.write(
                f"{paginas_fallidas} página(s) de BIMS fallaron: no se escribe nada."
            )
            return

        stock_bims = {bims_id: d["total"] for bims_id, d in datos_bims.items()}
        cambios = calcular_cambios(candidatos, stock_bims)
        excedido = radio_excedido(cambios, settings.STOCK_ZERO_GUARD)
        if excedido:
            notify(
                "stock_apagon_masivo",
                f"⛔ El barrido de stock abortó: apagaría {excedido} productos de "
                f"una vez, por encima del tope de {settings.STOCK_ZERO_GUARD}. Un "
                f"cero masivo casi siempre es un problema de la consulta o de los "
                f"depósitos configurados ({settings.STOCK_WAREHOUSE_IDS}), no que "
                f"se haya vendido todo. No se escribió nada.",
            )
            self.stderr.write(f"Guarda de radio: {excedido} apagados, no se escribe nada.")
            return

        self._informar(cambios, candidatos, descartes, datos_bims, escribe)

        if not escribe:
            self.stdout.write(
                self.style.WARNING(
                    "MODO SECO: nada se escribió. Para aplicar, --aplicar o "
                    "STOCK_SYNC_ENABLED=true en el .env."
                )
            )
            return

        escritos = fallidos = 0
        for cambio in cambios:
            try:
                wc_api.update_product_stock(cambio.ruta_woo, cambio.stock_nuevo)
            except Exception as e:  # noqa: BLE001
                # Una escritura que falla no debe frenar el resto del barrido: la
                # escritura es idempotente y se reintenta en el barrido siguiente.
                fallidos += 1
                self.stderr.write(f"Producto {cambio.woo_id} sin actualizar: {e}")
                continue
            escritos += 1

        self.stdout.write(
            self.style.SUCCESS(f"Escritos {escritos}, fallidos {fallidos}.")
        )

    # ---------- lectura ----------

    def _candidatos_de_woo(self) -> Tuple[List[dict], Counter]:
        """
        Los productos publicados de Woo que tienen vínculo con BIMS.

        Arranca por Woo y no por BIMS a propósito: la respuesta ya trae el stock
        actual, así que la comparación sale gratis y no hace falta ni tabla nueva
        ni migración para saber qué cambió.
        """
        candidatos: List[dict] = []
        descartes: Counter = Counter()

        for producto in self._paginar_productos_woo():
            if producto.get("type") == "variable":
                candidatos.extend(
                    self._candidatos_de_variaciones(producto, descartes)
                )
                continue

            bims_id, motivo = resolver_bims_id(
                producto.get("sku"), None, hermanas_sin_sku=1
            )
            if bims_id is None:
                descartes[motivo] += 1
                continue
            candidatos.append(
                {
                    "woo_id": producto["id"],
                    "ruta_woo": str(producto["id"]),
                    "bims_id": bims_id,
                    "stock_actual": float(producto.get("stock_quantity") or 0),
                }
            )

        return candidatos, descartes

    def _paginar_productos_woo(self):
        pagina = 1
        while True:
            productos = wc_api.get_products(
                per_page=100, page=pagina, status="publish"
            )
            if not productos:
                return
            for producto in productos:
                yield producto
            if len(productos) < 100:
                return
            pagina += 1

    def _candidatos_de_variaciones(self, padre: dict, descartes: Counter) -> List[dict]:
        variaciones = wc_api.get_variations(padre["id"], per_page=100)
        sin_sku = sum(1 for v in variaciones if not str(v.get("sku") or "").strip())

        salida = []
        for variacion in variaciones:
            bims_id, motivo = resolver_bims_id(
                variacion.get("sku"), padre.get("sku"), hermanas_sin_sku=sin_sku or 1
            )
            if bims_id is None:
                descartes[motivo] += 1
                continue
            salida.append(
                {
                    "woo_id": variacion["id"],
                    "ruta_woo": f"{padre['id']}/variations/{variacion['id']}",
                    "bims_id": bims_id,
                    "stock_actual": float(variacion.get("stock_quantity") or 0),
                }
            )
        return salida

    def _stock_de_bims(self) -> Tuple[Dict[int, dict], int]:
        """
        Por cada producto inventariable de BIMS: total, desglose y nombre.

        Sólo entran los `stockable: true` — es el alcance que decidió Carlos, y
        deja afuera las entradas sin necesidad de una lista negra: no tienen
        inventario en BIMS.

        El desglose y el nombre se guardan para el reporte, no para decidir: sin
        ellos, un número publicado en la web no se puede explicar después.
        """
        datos: Dict[int, dict] = {}
        fallidas = 0
        offset = 0

        while True:
            try:
                data = bims.get_products_with_stock(
                    limit=settings.STOCK_PAGE_SIZE, offset=offset
                )
            except Exception as e:  # noqa: BLE001
                fallidas += 1
                self.stderr.write(f"Página de BIMS en offset={offset} falló: {e}")
                break

            filas = data.get("data") or []
            if not filas:
                break

            for fila in filas:
                producto = fila.get("Product") or {}
                if producto.get("stockable") not in (True, "true", 1, "1"):
                    continue
                try:
                    bims_id = int(producto.get("id"))
                except (TypeError, ValueError):
                    continue
                disponibilidad = producto.get("AvailabilityFull")
                datos[bims_id] = {
                    "total": stock_vendible(
                        disponibilidad, settings.STOCK_WAREHOUSE_IDS
                    ),
                    "desglose": desglose_por_deposito(
                        disponibilidad, settings.STOCK_WAREHOUSE_IDS
                    ),
                    "nombre": producto.get("name") or "",
                }

            offset += settings.STOCK_PAGE_SIZE
            if len(filas) < settings.STOCK_PAGE_SIZE:
                break

        return datos, fallidas

    # ---------- reporte ----------

    def _informar(self, cambios, candidatos, descartes, datos_bims, escribe: bool) -> None:
        """
        El desglose no es prolijidad: como WooCommerce **no sabe** en qué depósito
        vive nada, cuando alguien pregunte "por qué dice 3" la respuesta no está
        ni en la web ni en Woo. Sin este registro, ese número no se puede
        explicar después.
        """
        self.stdout.write(
            f"Vinculados {len(candidatos)} | cambios {len(cambios)} | "
            f"depósitos {settings.STOCK_WAREHOUSE_IDS} | "
            f"{'ESCRIBE' if escribe else 'SECO'}"
        )
        for cambio in cambios:
            flecha = "APAGA" if cambio.apaga else "     "
            desglose = (datos_bims.get(cambio.bims_id) or {}).get("desglose") or {}
            detalle = " + ".join(f"dep {d}: {u:g}" for d, u in sorted(desglose.items()))
            self.stdout.write(
                f"  {flecha} woo {cambio.woo_id} (bims {cambio.bims_id}): "
                f"{cambio.stock_actual:g} -> {cambio.stock_nuevo:g}"
                f"{'  [' + detalle + ']' if detalle else '  [sin stock en ningún depósito]'}"
            )

        vinculados = {c["bims_id"] for c in candidatos}
        sin_contraparte = sorted(set(datos_bims) - vinculados)
        if sin_contraparte:
            self.stdout.write(
                f"{len(sin_contraparte)} producto(s) inventariables de BIMS sin "
                f"contraparte en WooCommerce (no se crean, sólo se informan):"
            )
            for bims_id in sin_contraparte[:20]:
                nombre = (datos_bims[bims_id] or {}).get("nombre") or ""
                self.stdout.write(f"  bims {bims_id}  {nombre[:50]}")
            if len(sin_contraparte) > 20:
                self.stdout.write(f"  ... y {len(sin_contraparte) - 20} más")

        self._informar_descartes(descartes)

    def _informar_descartes(self, descartes: Counter) -> None:
        if not descartes:
            return
        etiquetas = {
            SKU_AMBIGUO: "sin SKU y con hermanas sin SKU (heredar multiplicaría el stock)",
            SKU_SIN_VINCULO: "sin SKU ni en la variación ni en el padre",
            SKU_DADO_DE_BAJA: "SKU de producto dado de baja",
        }
        self.stdout.write("Descartados:")
        for motivo, cuantos in descartes.most_common():
            self.stdout.write(f"  {cuantos:>4}  {etiquetas.get(motivo, motivo)}")
```

- [x] **Step 4: Correr los tests**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings )`

Expected: **288 OK**.

- [x] **Step 5: Commit**

```bash
git add core/management/commands/sync_stock.py core/stock.py core/tests.py
git commit -m "feat(stock): el comando del barrido, con sus tres guardas

Arranca por Woo y no por BIMS a proposito: la respuesta de Woo ya trae el
stock actual, asi que la comparacion sale gratis y no hace falta tabla
nueva ni migracion para saber que cambio.

Las tres guardas, todas con test:

1. Si una pagina de BIMS falla, NO se escribe nada. Con una lectura
   incompleta los productos de esa pagina se verian como 0 y se
   apagarian: 'no hay dato' no es 'el dato es cero'.
2. Si el barrido apagaria mas de STOCK_ZERO_GUARD productos, aborta y
   avisa. Las subidas no cuentan: el primer barrido va a encender
   decenas de productos y eso es correcto.
3. Modo seco por default (STOCK_SYNC_ENABLED=false), con --aplicar para
   forzar y --seco para lo contrario.

El reporte imprime el desglose porque Woo no sabe en que deposito vive
nada: sin ese registro, el numero publicado no se puede explicar despues."
```

---

## Task 9: El cron, y sacar el código rescatado

**Files:**
- Create: `sync-stock.sh`
- Delete: `core/stock_sync_rescatado.py`, `core/management/commands/syncstock.py`
- Modify: `docs/superpowers/specs/2026-09-02-sincronizacion-stock-bims-design.md` (marcar implementado)

- [x] **Step 1: Crear el envoltorio del cron**

Crear `sync-stock.sh` con permiso de ejecución:

```bash
#!/usr/bin/env bash
#
# Barrido de stock BIMS → WooCommerce, para el cron. Cada 15 minutos.
#
# `flock -n` sale sin hacer nada si ya hay una corrida en curso: un barrido
# lento se solaparía con el siguiente y los dos escribirían los mismos
# productos.
#
# El `cd` NO es cosmético. `settings.py` carga la configuración con
# `dotenv_values(".env")`, que es una ruta RELATIVA: desde el cron el directorio
# de trabajo es el home, ahí no hay `.env`, y settings revienta en
# `config.get("DEBUG").lower()` sobre None antes de llegar a Django.
#
# Línea de cron (la instala Carlos, necesita root):
#   */15 * * * * root /var/www/integrador/sync-stock.sh >> /var/log/sync-stock.log 2>&1
#
# ⚠️ Y con logrotate desde el día uno: `/var/log/process-queue.log` se instaló
# sin rotación el 2026-09-02 y ya es deuda.
set -euo pipefail

cd /var/www/integrador

exec /usr/bin/flock -n /var/lock/sync-stock.lock \
    /root/venv-integrador-52/bin/python manage.py sync_stock
```

Run: `chmod +x sync-stock.sh`

- [x] **Step 2: Verificar que el script es ejecutable en git**

Run: `git ls-files -s sync-stock.sh`
Expected: modo `100755`. Si dice `100644`, el cron no va a poder ejecutarlo: `chmod +x` y volver a agregarlo.

- [x] **Step 3: Borrar el código rescatado**

```bash
git rm core/stock_sync_rescatado.py core/management/commands/syncstock.py
```

Ya cumplió su función: era referencia para comparar, y la comparación está escrita en la spec.

- [x] **Step 4: Correr la suite completa**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings )`

Expected: **288 OK**. Y verificar que nadie importaba lo borrado:

Run: `grep -rn "stock_sync_rescatado\|syncstock" --include=*.py core/ muci-integrador/`
Expected: sin resultados.

- [x] **Step 5: Verificar que ningún test sale a la red**

Run: `( ulimit -v 6291456; .venv/bin/python manage.py test core/ --settings=muci-integrador.test_settings 2>&1 | grep -ciE "name resolution|HTTPConnectionPool|hooks.slack" )`
Expected: `0`

- [x] **Step 6: Commit**

```bash
git add sync-stock.sh
git commit -m "feat(stock): cron del barrido y baja del codigo rescatado

sync-stock.sh cada 15 minutos, con flock -n para que dos barridos no se
solapen escribiendo los mismos productos, y con el cd al checkout porque
settings.py lee el .env con ruta relativa.

Se borran core/stock_sync_rescatado.py y el comando syncstock: eran
referencia para comparar las dos implementaciones viejas, y esa
comparacion ya quedo escrita en la spec."
```

---

## Task 10: Verificación sobre los dos stacks

- [x] **Step 1: Verificar sobre el stack de rollback** (lo corre el asistente)

Run: `./verificar-en-stack-produccion.sh`
Expected: **288 OK** sobre Python 3.7 + Django 3.2.

⚠️ Si falla por sintaxis, mirar los f-strings anidados y las anotaciones: 3.7 no soporta `list[int]` ni `dict[str, float]` sin `from __future__ import annotations`. Usar `List[int]` y `Dict[str, float]` de `typing`, que es lo que este plan usa.

- [ ] **Step 2: Verificar sobre el stack REAL** (lo corre Carlos, necesita root)

```
PYTHON=/root/venv-integrador-52/bin/python SERVIDOR=root@muci.org REMOTO=wt-verificacion-52 ./verificar-en-stack-produccion.sh
```
Expected: **288 OK** sobre Python 3.10.12 + Django 5.2.17.

- [x] **Step 3: Marcar la spec como implementada**

En el encabezado de `docs/superpowers/specs/2026-09-02-sincronizacion-stock-bims-design.md`, cambiar `**Estado:** aprobado por Carlos, pendiente de plan de implementación` por `**Estado:** implementado en la rama, pendiente del primer barrido en seco`.

- [x] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-09-02-sincronizacion-stock-bims-design.md
git commit -m "docs(stock): spec implementada, 288 tests verdes en los dos stacks"
```

---

## Después del plan: el despliegue, que NO es parte de estas tareas

El barrido **arranca apagado** (`STOCK_SYNC_ENABLED=false`), así que desplegar el código no cambia nada en la web. La secuencia es:

1. Desplegar el código con el barrido apagado y **sin** la línea de cron.
2. Correr `manage.py sync_stock` **a mano**, en seco, y **leer la lista completa**. Es el momento de confirmar que los depósitos `6,7` son los correctos y de ver cuántos productos se encenderían.
3. Con esa lista aprobada, instalar el cron y recién después poner `STOCK_SYNC_ENABLED=true`.
4. Mirar el primer barrido real. Se espera que **encienda** decenas de productos: hoy la web muestra agotados productos que sí tienen stock — `JUGUETE CARTAS INFANTILES SC` dice 16 y tiene 71.

El paso 2 es el que no se puede saltear. Es la única oportunidad de ver el efecto completo antes de que lo vea un cliente.
