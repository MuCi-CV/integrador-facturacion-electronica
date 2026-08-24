import urllib

from woocommerce import API as WCAPI
from django.conf import settings
from typing import Union, List, Optional


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
            url, key, secret, version="wc/v3", timeout=480, verify_ssl=verify_ssl
        )

    def get_products(self, **kwargs):
        res = self.wcapi.get("products", params=kwargs)
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)

    def get_product(self, id, **kwargs):
        res = self.wcapi.get(f"products/{id}", params=kwargs)
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)

    def get_order(self, id, **kwargs):
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
        res = self.wcapi.get("customers", params={"email": email, "role": "all"})
        if res.status_code != 200:
            raise self.ServerException(res.text)
        encontrados = res.json()
        return encontrados[0] if encontrados else None

    def refund_order(self, id, data, **kwargs):
        res = self.wcapi.post(f"orders/{id}/refunds", data={"data": data})
        if res.status_code == 200:
            return res.json()
        raise self.ServerException(res.text)


wc_api = WooCommerceAPI()
