import urllib

from woocommerce import API as WCAPI
from django.conf import settings
from typing import Union, List

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

        self.wcapi = WCAPI(
            url, key, secret, version="wc/v3", timeout=480, verify_ssl=False
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
wc_api = WooCommerceAPI()