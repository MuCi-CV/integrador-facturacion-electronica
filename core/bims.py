import requests
import logging
from django.conf import settings
from typing import Optional


class BimsApi:
    def __init__(self) -> None:
        self.base_url = settings.BIMS_URL
        self.sid = self.login()

    def login(self) -> Optional[str]:
        url = f"{self.base_url}/users/login/"
        body = {
            "user": settings.BIMS_USER,
            "password": settings.BIMS_PASSWORD,
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

    def create_contact(
        self,
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
            res = requests.post(url=url, json=body, params=params)
            response_data = res.json()
            if response_data.get("status") == "ok":
                return response_data.get("data").get("Contact").get("id")
            else:
                logging.error("BIMS create contact error.")
                logging.error(response_data)
        except requests.RequestException as e:
            logging.error("BIMS create contact error.")
            logging.error(str(e))
            raise e

    def create_sale(self, contact_id, product_id, price, notes):
        url = f"{self.base_url}/sales/"
        body = {
            "Sale": {
                "invoice_number": "auto",
                "contact_id": contact_id,
                "company_id": 1,
            },
            "SalesProduct": [
                {
                    "product_id": product_id,
                    "quantity": 1.00,
                    "price": str(price),
                    "notes": notes,
                }
            ],
        }
        params = {"sid": self.sid}
        try:
            res = requests.post(url=url, json=body, params=params)
            response_data = res.json()
            if response_data.get("status") == "ok":
                return response_data.get("data").get("Sale").get("id")
        except requests.RequestException as e:
            logging.error("BIMS create sale error.")
            logging.error(str(e))
            raise e

    def send_invoice(self, sale_id):
        url = f"{self.base_url}/sales/send/{sale_id}/"
        params = {"sid": self.sid}
        try:
            res = requests.get(url=url, params=params)
            response_data = res.json()
            if response_data.get("status") == "ok":
                return "ok"
        except requests.RequestException as e:
            logging.error("BIMS send invoice error.")
            logging.error(str(e))
            raise e

bims = BimsApi()