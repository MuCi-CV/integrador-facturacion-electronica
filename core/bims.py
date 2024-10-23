import requests
import logging
from django.conf import settings
from typing import Optional
import hashlib
from typing import Any


class BimsApi:
    def __init__(self) -> None:
        self.base_url = settings.BIMS_URL
        self.sid = self.login()

    def login(self) -> Optional[str]:
        url = f"{self.base_url}/users/login/"
        body = {
            "user": settings.BIMS_USER,
            "password": hashlib.md5(settings.BIMS_PASSWORD.encode()).hexdigest(),
            "tenant": settings.BIMS_TENANT,
        }

        try:
            res = requests.post(url=url, json=body)
            response_data = res.json()
            if response_data.get("status") == "ok":
                return response_data.get("data").get("Session").get("id")
        except requests.RequestException as e:
            logging.error("Login BIMS error.")
            logging.error(str(e))
            raise e

    def _request_with_relogin(self, method, url, **kwargs):
        """
        Hace una solicitud y, si recibe un 401, renueva el sid y vuelve a intentar.
        """
        try:
            res = method(url, **kwargs)
            if res.status_code == 401:
                logging.info("Session expired, attempting relogin...")
                self.sid = self.login()  # Intentar relogin
                if self.sid:
                    # Actualizamos los parámetros con el nuevo sid y repetimos la solicitud
                    kwargs["params"]["sid"] = self.sid
                    res = method(url, **kwargs)
            res.raise_for_status()
            return res.json()
        except requests.RequestException as e:
            logging.error(f"Error during {method.__name__.upper()} request to {url}.")
            logging.error(str(e))
            raise e

    def list_contacts(self, document_id: str, document_type: str):
        url = f"{self.base_url}/contacts/"
        params = {
            "sid": self.sid,
            "document_id": document_id,
            "document_type": document_type,
        }
        try:
            response_data = self._request_with_relogin(requests.get, url, params=params)
            if int(response_data.get("count")) > 0:
                return int(response_data.get("data")[0].get("Contact").get("id"))
            return None
        except requests.RequestException as e:
            logging.error("BIMS get contact error.")
            logging.error(str(e))
            raise e

    def create_contact(
        self,
        id: Any,
        name: str,
        document_id: str,
        document_type: str,
        address: str,
        phones,
        emails: str,
    ) -> str:
        url = f"{self.base_url}/contacts/"
        body = {
            "Contact": {
                "id": id,
                "name": name,
                "document_id": document_id,
                "document_type": document_type,
                "address": address,
                "phones": phones,
                "emails": emails,
            }
        }
        params = {"sid": self.sid}
        try:
            response_data = self._request_with_relogin(
                requests.post, url, json=body, params=params
            )
            if response_data.get("status") == "ok":
                return response_data.get("data").get("Contact").get("id")
        except requests.RequestException as e:
            logging.error("BIMS create contact error.")
            logging.error(str(e))
            raise e

    def create_sale(
        self,
        contact_id,
        sale_products,
        posale_id,
        payment_method_id,
        amount,
        contact_emails,
        order,
    ):
        url = f"{self.base_url}/sales/"
        body = {
            "Sale": {
                "invoice_number": "auto",
                "contact_id": contact_id,
                "company_id": 1,
                "posale_id": posale_id,
                "contact_emails": contact_emails,
                "_id": order,
                # "billed": False,
            },
            "SalesProduct": sale_products,
            "SalesPaymentMethod": [
                {"payment_method_id": payment_method_id, "amount": amount}
            ],
        }
        params = {"sid": self.sid}
        try:
            res = self._request_with_relogin(
                requests.post, url, json=body, params=params
            )
            if res.get("status") == "ok":
                return res.get("data").get("Sale").get("id")
        except requests.RequestException as e:
            logging.error("BIMS create sale error.")
            logging.error(str(e))
            raise e

    def send_invoice(self, sale_id):
        url = f"{self.base_url}/sales/send/{sale_id}/"
        params = {"sid": self.sid}
        try:
            res = self._request_with_relogin(requests.get, url, params=params)
            response_data = res.json()
            if response_data.get("status") == "ok":
                return "ok"
        except requests.RequestException as e:
            logging.error("BIMS send invoice error.")
            logging.error(str(e))
            raise e


bims = BimsApi()
