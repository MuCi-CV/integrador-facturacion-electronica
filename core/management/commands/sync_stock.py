"""
Barrido de stock BIMS → WooCommerce. Corre por cron cada 15 minutos.

**No es un evento.** BIMS no tiene webhook de salida y ningún endpoint de stock
acepta un filtro "modificado desde", así que "cuando ajustan en BIMS" sólo puede
implementarse sondeando. El peor desfase es un cuarto de hora.

Toda la lógica de decisión vive en `core/stock.py`, en funciones puras. Acá sólo
hay I/O y orquestación.
"""

from collections import Counter
from typing import Dict, List, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand

from core.alerts import notify
from core.bims import bims
from core.stock import (
    SKU_DADO_DE_BAJA,
    SKU_SIN_VINCULO,
    calcular_cambios,
    colisiones,
    desglose_por_deposito,
    destinos_de_producto,
    radio_excedido,
    stock_vendible,
)
from core.woocommerce import wc_api

PAGINA_WOO = 100


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

        destinos, descartes, ignoradas = self._destinos_de_woo()
        if not destinos:
            self.stdout.write("Ningún producto de WooCommerce quedó vinculado.")
            self._informar_descartes(descartes)
            return

        choques = colisiones(destinos)
        if choques:
            # No debería pasar con "un producto de BIMS, un destino". Si pasa, hay
            # un SKU propio repetido en Woo y escribir multiplicaría el inventario.
            detalle = "; ".join(
                f"bims {bims_id} ← "
                + ", ".join(f"woo {d.woo_id}" for d in ds)
                for bims_id, ds in sorted(choques.items())
            )
            notify(
                "stock_colision_de_sku",
                f"⛔ El barrido de stock abortó: {len(choques)} producto(s) de BIMS "
                f"están reclamados por más de un destino de WooCommerce, así que "
                f"publicar duplicaría su inventario. Revisar los SKU repetidos: "
                f"{detalle}",
            )
            self.stderr.write(f"Colisión de SKU en {len(choques)}: no se escribe nada.")
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
        cambios = calcular_cambios(destinos, stock_bims)

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
            self.stderr.write(
                f"Guarda de radio: {excedido} apagados, no se escribe nada."
            )
            return

        self._informar(cambios, destinos, descartes, ignoradas, datos_bims, escribe)

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

    def _destinos_de_woo(self):
        """
        Los destinos de escritura de Woo: **uno por producto de BIMS**.

        Arranca por Woo y no por BIMS a propósito: la respuesta ya trae el stock
        actual, así que la comparación sale gratis y no hace falta ni tabla nueva
        ni migración para saber qué cambió.

        ⚠️ **No se filtra por `status="publish"`**, y no es un olvido: **295 de las
        318 variaciones con SKU propio cuelgan de padres `private`** (medido el
        2026-09-03), porque así vive el catálogo del POS de FooEvents. Pedir sólo
        `publish` las dejaba afuera a todas, y ésa era la causa real de los "422
        productos inventariables de BIMS sin contraparte". El filtro por estado
        vendible lo hace `core.stock.ESTADOS_VENDIBLES`, que sí deja afuera
        `draft` y `trash`.
        """
        destinos = []
        descartes: Counter = Counter()
        ignoradas = []

        for producto in self._paginar_productos_woo():
            variaciones = (
                wc_api.get_variations(producto["id"], per_page=PAGINA_WOO)
                if producto.get("type") == "variable"
                else []
            )
            unos, esos_descartes, esas_ignoradas = destinos_de_producto(
                producto, variaciones
            )
            destinos.extend(unos)
            descartes.update(esos_descartes)
            ignoradas.extend(esas_ignoradas)

        return destinos, descartes, ignoradas

    def _paginar_productos_woo(self):
        pagina = 1
        while True:
            productos = wc_api.get_products(per_page=PAGINA_WOO, page=pagina)
            if not productos:
                return
            for producto in productos:
                yield producto
            if len(productos) < PAGINA_WOO:
                return
            pagina += 1

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

    def _informar(
        self, cambios, destinos, descartes, ignoradas, datos_bims, escribe: bool
    ) -> None:
        """
        El desglose no es prolijidad: como WooCommerce **no sabe** en qué depósito
        vive nada, cuando alguien pregunte "por qué dice 3" la respuesta no está
        ni en la web ni en Woo. Sin este registro, ese número no se puede
        explicar después.
        """
        self.stdout.write(
            f"Vinculados {len(destinos)} | cambios {len(cambios)} | "
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
                f"  [{detalle or 'sin stock en ningún depósito habilitado'}]"
                f"  {cambio.etiqueta[:40]}"
            )

        self._informar_flips(cambios)
        self._informar_ignoradas(ignoradas)

        vinculados = {d.bims_id for d in destinos}
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

    def _informar_flips(self, cambios) -> None:
        """
        Los destinos que pasarían de **venta ilimitada a limitada**.

        `update_product_stock` escribe `manage_stock: True`, así que un producto
        que hoy no gestiona stock queda gobernado por el número de BIMS. Son 14 de
        los 26 padres (medido 2026-09-03). Es inherente a publicar stock —no se
        puede publicar sin gestionar—, pero tiene que verse en el seco antes de
        aplicarlo, porque cambia cómo se vende.
        """
        flips = [c for c in cambios if not c.gestiona_stock]
        if not flips:
            return
        self.stdout.write(
            f"⚠️  {len(flips)} destino(s) pasarían de ILIMITADO a limitado "
            f"(hoy no gestionan stock):"
        )
        for cambio in flips:
            self.stdout.write(
                f"  woo {cambio.woo_id} (bims {cambio.bims_id}) -> "
                f"{cambio.stock_nuevo:g}  {cambio.etiqueta[:45]}"
            )

    def _informar_ignoradas(self, ignoradas) -> None:
        """
        Variaciones que heredan el SKU del padre y llevan su **propio** contador.

        **No es un defecto y no hay que arreglarlo** (Carlos, 2026-09-03): esos
        contadores los mantiene la cajera, porque BIMS no modela el diseño —tiene
        un producto por tipo de taza, no por diseño— y ese detalle sólo existe del
        lado de Woo. La regla es: **el barrido escribe el stock del padre y no
        toca el de los hijos.**

        Se informan igual, por una razón: WooCommerce usa el contador de la
        variación, así que el número del padre **no gobierna** esas ventas. Sin
        esta lista, alguien podría leer el stock del padre como si fuera lo
        vendible y planificar con un número que no manda.
        """
        if not ignoradas:
            return
        self.stdout.write(
            f"{len(ignoradas)} variación(es) llevan su propio contador de stock, "
            f"que el barrido NO toca (lo mantiene la cajería). Para éstas, el "
            f"número del padre no gobierna la venta:"
        )
        for ignorada in ignoradas:
            self.stdout.write(
                f"  woo {ignorada.woo_id} (padre {ignorada.padre_id}, "
                f"bims {ignorada.bims_id}, stock {ignorada.stock_actual:g})  "
                f"{ignorada.etiqueta[:40]}"
            )

    def _informar_descartes(self, descartes: Counter) -> None:
        if not descartes:
            return
        etiquetas = {
            SKU_SIN_VINCULO: "sin SKU propio y sin SKU en el padre",
            SKU_DADO_DE_BAJA: "SKU de producto dado de baja",
        }
        self.stdout.write("Descartados:")
        for motivo, cuantos in descartes.most_common():
            self.stdout.write(f"  {cuantos:>4}  {etiquetas.get(motivo, motivo)}")
