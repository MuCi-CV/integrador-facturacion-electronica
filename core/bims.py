import requests
import logging
import json
from django.conf import settings
from typing import Optional, Any
import hashlib
import time
import sentry_sdk
import inspect

logging.basicConfig(
    filename="app.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# Logger dedicado para comunicación con BIMS API
bims_logger = logging.getLogger("bims_api")


def _get_caller_name():
    """Obtiene el nombre del método que originó la llamada a BIMS."""
    frame = inspect.currentframe()
    # Subir en el stack: _get_caller_name -> _request_with_relogin -> _retry_request -> método público
    for _ in range(4):
        if frame is not None:
            frame = frame.f_back
    return frame.f_code.co_name if frame else "unknown"


def _safe_json(obj):
    """Serializa un objeto a JSON de forma segura para logging."""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)


def _mask_params(params):
    """Enmascara el sid en los params para no loguear credenciales."""
    if not params:
        return params
    masked = dict(params)
    if "sid" in masked:
        masked["sid"] = "***"
    return masked


def _mask_login_body(body):
    """Enmascara password y tenant en el body de login."""
    if not body:
        return body
    masked = dict(body)
    if "password" in masked:
        masked["password"] = "***"
    return masked


class BimsApi:
    def __init__(self) -> None:
        self.base_url = settings.BIMS_URL
        self.sid = self.login()
        self.session = requests.Session()

    def login(self) -> Optional[str]:
        url = f"{self.base_url}/users/login/"
        body = {
            "user": settings.BIMS_USER,
            "password": hashlib.md5(settings.BIMS_PASSWORD.encode()).hexdigest(),
            "tenant": settings.BIMS_TENANT,
        }

        bims_logger.info("══════ BIMS REQUEST ══════")
        bims_logger.info(f"POST {url} | Caller: login")
        bims_logger.info(f"Body: {_safe_json(_mask_login_body(body))}")

        start_time = time.time()
        try:
            res = requests.post(url=url, json=body)
            elapsed = time.time() - start_time
            response_data = res.json()

            bims_logger.info("══════ BIMS RESPONSE ══════")
            bims_logger.info(f"Status: {res.status_code} | Time: {elapsed:.2f}s")
            bims_logger.info(f"Body: {_safe_json(response_data)}")

            if response_data.get("status") == "ok":
                return response_data.get("data").get("Session").get("id")
        except requests.RequestException as e:
            elapsed = time.time() - start_time
            bims_logger.error("══════ BIMS ERROR ══════")
            bims_logger.error(f"Login failed | Time: {elapsed:.2f}s | Error: {str(e)}")
            logging.error("Login BIMS error.")
            logging.error(str(e))
            raise e

    def _request_with_relogin(self, method, url, **kwargs):
        caller = _get_caller_name()
        method_name = method.__name__.upper()
        params = kwargs.get("params", {})
        body = kwargs.get("json", None)

        bims_logger.info("══════ BIMS REQUEST ══════")
        bims_logger.info(f"{method_name} {url} | Caller: {caller}")
        bims_logger.info(f"Params: {_safe_json(_mask_params(params))}")
        if body is not None:
            bims_logger.info(f"Body: {_safe_json(body)}")

        start_time = time.time()
        try:
            res = method(url, **kwargs)
            elapsed = time.time() - start_time

            if res.status_code == 401:
                bims_logger.warning(f"Session expired (401) | Time: {elapsed:.2f}s | Attempting relogin...")
                logging.info("Session expired, attempting relogin...")
                self.sid = self.login()
                if self.sid:
                    kwargs["params"]["sid"] = self.sid
                    bims_logger.info("══════ BIMS RETRY (after relogin) ══════")
                    bims_logger.info(f"{method_name} {url} | Caller: {caller}")
                    start_time = time.time()
                    res = method(url, **kwargs)
                    elapsed = time.time() - start_time

            response_body = res.json()
            bims_logger.info("══════ BIMS RESPONSE ══════")
            bims_logger.info(f"Status: {res.status_code} | Time: {elapsed:.2f}s")
            bims_logger.info(f"Body: {_safe_json(response_body)}")

            res.raise_for_status()
            return response_body
        except requests.RequestException as e:
            elapsed = time.time() - start_time
            error_msg = f"Error during {method_name} request to {url}."
            if hasattr(e, 'response') and e.response is not None:
                error_msg += f" Response status: {e.response.status_code}. Response body: {e.response.text}"
                bims_logger.error("══════ BIMS ERROR ══════")
                bims_logger.error(f"{method_name} {url} | Time: {elapsed:.2f}s")
                bims_logger.error(f"Status: {e.response.status_code}")
                bims_logger.error(f"Response: {e.response.text}")
            else:
                bims_logger.error("══════ BIMS ERROR ══════")
                bims_logger.error(f"{method_name} {url} | Time: {elapsed:.2f}s | Error: {str(e)}")
            logging.error(error_msg)
            logging.error(str(e))
            raise Exception(error_msg) from e


    def _retry_request(self, method, url, max_retries=5, retry_delay=2, **kwargs):
        attempts = 0
        while attempts < max_retries:
            try:
                response_data = self._request_with_relogin(method, url, **kwargs)
                if response_data.get("status") == "ok":
                    return response_data
                else:
                    attempts += 1
                    err_message = f"Attempt {attempts} failed: status not 'ok'. Response: {response_data}"
                    last_error_details = err_message
                    logging.warning(err_message + " Retrying...")
                    time.sleep(retry_delay)
            except Exception as e:
                err_message = f"Error in request to {url}. Attempt {attempts + 1} of {max_retries}. Error: {str(e)}"
                last_error_details = err_message
                logging.error(err_message)
                attempts += 1
                time.sleep(retry_delay)
        raise Exception(f"Failed request to {url} after {max_retries} attempts. Last error: {last_error_details}")

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

    ## buscar ruc en turuc
    def find_razon_social_by_ruc(self, document_id: str):
        url = f"{self.ruc_url}/contribuyente/"
        params = {
            "ruc": document_id
        }
        response_data = self._retry_request(requests.get, url, params=params)
        if int(response_data.get("count")) > 0:
            return response_data.get("data")[0].get("Contact").get("name")
        return None

    def find_contact_by_ruc(self, document_id: str, document_type: str):
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

    def find_contact_by_email(self, email: str):
        url = f"{self.base_url}/contacts/"
        params = {
            "sid": self.sid,
            "email": email,
        }

        response_data = self._retry_request(requests.get, url, params=params)
        if int(response_data.get("count")) > 0:
            return int(response_data.get("data")[0].get("Contact").get("id"))
        return None

    def find_contact_by_name(self, name: str, email: str = None, document_id: str = None):
        url = f"{self.base_url}/contacts/"
        params = {
            "sid": self.sid,
            "q": name,
        }
        
        response_data = self._retry_request(requests.get, url, params=params)

        if not email and not document_id:
            raise ValueError("Debes proporcionar al menos el 'email' o el 'document_id' para validar el contacto.")

        if int(response_data.get("count")) > 0:
            for item in response_data.get("data"):
                contact = item.get("Contact", {})
                if contact.get("document_id") == document_id:
                    return int(contact.get("id"))
                if contact.get("emails") == email:
                    return int(contact.get("id"))
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
                "company_id": 1,
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
        
        data = response_data.get("data")
        if isinstance(data, dict) and data.get("Sale") and data["Sale"].get("id"):
            return data["Sale"]["id"], None
            
        error_msg = response_data.get("message") or response_data.get("error") or "BIMS devolvió HTTP 200 pero no generó la venta (ID vacío)."
        return None, error_msg

    def send_invoice(self, sale_id):
        url = f"{self.base_url}/sales/send/{sale_id}/"
        params = {"sid": self.sid}
        response_data = self._retry_request(requests.get, url, params=params)
        return "ok" if response_data.get("status") == "ok" else None

    def get_contacts(self, limit=500, offset=0):
        url = f"{self.base_url}/contacts/"
        params = {
            "sid": self.sid,
            "limit": limit,
            "offset": offset,
        }
        return self._retry_request(requests.get, url, params=params)


bims = BimsApi()
