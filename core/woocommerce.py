import contextlib
import urllib

from woocommerce import API as WCAPI
from django.conf import settings
from typing import Union, List, Optional, Tuple

from core import deadline


# Estaba en 480 s: cuatro veces los `--timeout 120` de gunicorn. Cualquier
# llamada que pasara los 120 s hacía que gunicorn matara al worker POR SEÑAL, y
# un worker matado por señal no ejecuta el `except` que graba el FailedOrder: la
# orden se perdía sin factura y sin registro. Ojo que `get_product` se llama una
# vez por ítem de la orden, así que este valor se multiplica por la cantidad de
# ítems; WooCommerce vive en el mismo servidor y responde rápido.
TIMEOUT_WOOCOMMERCE = 30

# Espejo de `bims.TIMEOUT_CONEXION`. Solo se aplica cuando hay presupuesto de
# orden: un timeout escalar en `requests` vale para conexión Y lectura, así que
# sin la tupla una llamada recortada a N segundos puede gastar 2N.
TIMEOUT_CONEXION_WOOCOMMERCE = 5


class WooCommerceAPI:
    """
    Provides services to interact with a WooCommerce website
    """

    class ServerException(Exception):
        """Raise when there is an error with WooCommerce API"""

        def __init__(self, message="Unexpected error in Woocommerce."):
            self.message = message
            super().__init__(self.message)

    def __init__(
        self, api_url: str = None, consumer_key: str = None, consumer_secret: str = None
    ):
        url = api_url or getattr(settings, "WOOCOMMERCE_URL")
        key = consumer_key or getattr(settings, "WOOCOMMERCE_KEY")
        secret = consumer_secret or getattr(settings, "WOOCOMMERCE_SECRET")

        assert url is not None, "WooCommerce URL not found"
        assert key is not None, "WooCommerce Key not found"
        assert secret is not None, "WooCommerce Secret not found"

        verify_ssl = getattr(settings, "WOOCOMMERCE_VERIFY_SSL", True)
        self.wcapi = WCAPI(
            url, key, secret, version="wc/v3", timeout=TIMEOUT_WOOCOMMERCE, verify_ssl=verify_ssl
        )

    def _timeout_efectivo(self, endpoint: str) -> Union[float, Tuple[float, float]]:
        """
        Timeout de esta llamada: el mínimo entre el propio y lo que queda de la orden.

        `woocommerce.API` guarda `self.timeout` como atributo de instancia y lo lee
        en cada request, así que ajustarlo antes de llamar surte efecto.

        Sin presupuesto de orden devuelve el timeout de siempre, como escalar: eso
        mantiene byte a byte todo uso fuera de `process_order` — en particular
        `sync_bims_contacts`, que nunca fija presupuesto y no debe cambiar de
        comportamiento.

        Con presupuesto de orden en juego devuelve una tupla `(conexión, lectura)`
        en vez de un escalar: un timeout escalar en `requests` vale para conexión
        Y lectura por separado, así que una llamada recortada a N segundos podía
        tardar hasta 2N (mismo defecto ya corregido del lado de BIMS, ver
        `TIMEOUT_CONEXION` en `core/bims.py`).

        `endpoint` solo se usa para el mensaje si el presupuesto ya se agotó: sin
        eso, `FailedOrder.message` no distinguía qué llamada de WooCommerce se
        quedó sin tiempo (hallazgo 4).

        OJO concurrencia: el valor calculado acá se asigna a `self.wcapi.timeout`,
        que es estado mutable de instancia sobre `wc_api`, el singleton a nivel de
        módulo (ver el final de este archivo). Es exactamente el patrón que el
        docstring de `core/deadline.py` descarta para el singleton `bims` ("se
        rompe sin aviso el día que algo sea concurrente") — acá se acepta porque
        `woocommerce.API` únicamente expone el timeout como atributo mutable de
        instancia, no hay otra forma de variarlo por llamada sin envolver la
        librería. Es seguro solo porque gunicorn corre workers **sincrónicos**:
        un worker atiende un request a la vez, así que no hay dos requests en
        vuelo que puedan pisarse la asignación. Con una clase de worker con
        concurrencia dentro del proceso (`gthread` con `--threads > 1`, `gevent`)
        dos órdenes podrían intercalarse entre la asignación y el `.get()`/`.post()`
        y una terminaría usando el presupuesto de la otra, en silencio. Si algún
        día se cambia la clase de worker, este método deja de ser seguro tal como
        está escrito.
        """
        restante = deadline.restante()
        if restante is None:
            return TIMEOUT_WOOCOMMERCE
        if restante <= 0:
            raise deadline.PresupuestoOrdenAgotado(
                f"Presupuesto de orden agotado antes de llamar a WooCommerce en "
                f"{endpoint}, excedido por {-restante:.1f}s."
            )
        return (
            min(TIMEOUT_CONEXION_WOOCOMMERCE, restante),
            min(TIMEOUT_WOOCOMMERCE, restante),
        )

    @contextlib.contextmanager
    def _timeout_recortado(self, endpoint: str):
        """
        Fija `self.wcapi.timeout` al recorte de esta llamada y lo restaura al salir.

        Sin el restore, el recorte queda pegado al singleton `wc_api`: el valor
        (posible fracción de segundo) que dejó la última orden persiste ahí hasta
        que otra llamada lo vuelva a asignar. Los cinco métodos que usan esto se
        auto-sanan porque todos reasignan antes de pegarle a la API, pero un
        método nuevo que se agregue sin pasar por acá (como `get_products`, que
        hoy no lo usa) heredaría en silencio el presupuesto de una orden anterior.
        """
        self.wcapi.timeout = self._timeout_efectivo(endpoint)
        try:
            yield
        finally:
            self.wcapi.timeout = TIMEOUT_WOOCOMMERCE

    def get_products(self, **kwargs):
        res = self.wcapi.get("products", params=kwargs)
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)

    def get_product(self, id, **kwargs):
        with self._timeout_recortado(f"products/{id}"):
            res = self.wcapi.get(f"products/{id}", params=kwargs)
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)

    def get_order(self, id, **kwargs):
        with self._timeout_recortado(f"orders/{id}"):
            res = self.wcapi.get(f"orders/{id}", params=kwargs)
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)

    def get_customer(self, id, **kwargs) -> Optional[dict]:
        """
        Usuario de WordPress por id. Devuelve None si no existe.

        Los cajeros de FooEvents POS son "customers" de wc/v3 con rol
        `fooeventspos_cashier` (verificado el 2026-08-24: customers/729 ->
        sancosmos@muci.org), así que no hace falta la API de WordPress.
        """
        with self._timeout_recortado(f"customers/{id}"):
            res = self.wcapi.get(f"customers/{id}", params=kwargs)
        if res.status_code == 200:
            return res.json()
        if res.status_code == 404:
            return None
        raise self.ServerException(res.text)

    def find_customer_by_email(self, email: str) -> Optional[dict]:
        """
        Usuario de WordPress por email. Devuelve None si no hay coincidencia.

        Hace falta `role=all`: sin eso wc/v3 solo devuelve los que tienen rol
        `customer` y los cajeros quedan afuera.
        """
        with self._timeout_recortado("customers"):
            res = self.wcapi.get("customers", params={"email": email, "role": "all"})
        if res.status_code != 200:
            raise self.ServerException(res.text)
        encontrados = res.json()
        return encontrados[0] if encontrados else None

    def refund_order(self, id, data, **kwargs):
        with self._timeout_recortado(f"orders/{id}/refunds"):
            res = self.wcapi.post(f"orders/{id}/refunds", data={"data": data})
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)


wc_api = WooCommerceAPI()
