import requests
import logging
from django.conf import settings
from typing import Optional, Any
import hashlib
import time

logging.basicConfig(
    filename="app.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


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
        try:
            res = method(url, **kwargs)
            if res.status_code == 401:
                logging.info("Session expired, attempting relogin...")
                self.sid = self.login()  # Intentar relogin
                if self.sid:
                    kwargs["params"]["sid"] = self.sid
                    res = method(url, **kwargs)
            res.raise_for_status()
            return res.json()
        except requests.RequestException as e:
            logging.error(f"Error during {method.__name__.upper()} request to {url}.")
            logging.error(str(e))
            raise e

    def _retry_request(self, method, url, max_retries=5, retry_delay=2, **kwargs):
        attempts = 0
        while attempts < max_retries:
            try:
                response_data = self._request_with_relogin(method, url, **kwargs)
                if response_data.get("status") == "ok":
                    return response_data
                else:
                    attempts += 1
                    logging.warning(
                        f"Attempt {attempts} failed: status not 'ok'. Retrying..."
                    )
                    time.sleep(retry_delay)
            except requests.RequestException as e:
                logging.error(
                    f"Error in request to {url}. Attempt {attempts + 1} of {max_retries}."
                )
                logging.error(str(e))
                attempts += 1
                time.sleep(retry_delay)
        raise Exception(f"Failed request to {url} after {max_retries} attempts.")

    def list_contacts(self, document_id: str, document_type: str):
        url = f"{self.base_url}/contacts/"
        params = {
            "sid": self.sid,
            "document_id": document_id,
            "document_type": document_type,
        }
        response_data = self._retry_request(requests.get, url, params=params)
        if int(response_data.get("count")) > 0:
            return int(response_data.get("data")[0].get("Contact").get("id"))
        return None

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
        response_data = self._retry_request(
            requests.post, url, json=body, params=params
        )
        return response_data.get("data").get("Contact").get("id")

    def create_sale(
        self,
        contact_id,
        sale_products,
        posale_id,
        sales_payment_methods,
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
            },
            "SalesProduct": sale_products,
            "SalesPaymentMethod": sales_payment_methods,
        }
        params = {"sid": self.sid}
        response_data = self._retry_request(
            requests.post, url, json=body, params=params
        )
        return response_data.get("data").get("Sale").get("id")

    def send_invoice(self, sale_id):
        url = f"{self.base_url}/sales/send/{sale_id}/"
        params = {"sid": self.sid}
        response_data = self._retry_request(requests.get, url, params=params)
        return "ok" if response_data.get("status") == "ok" else None


bims = BimsApi()
