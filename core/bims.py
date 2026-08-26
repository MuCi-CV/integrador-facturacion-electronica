import requests
import logging
import json
from django.conf import settings
from http import cookiejar
from typing import Optional, Any
import hashlib
import time
import sentry_sdk
import inspect

from core import deadline

logging.basicConfig(
    filename="app.log",
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# Logger dedicado para comunicación con BIMS API
bims_logger = logging.getLogger("bims_api")

# ── Límites de tiempo ────────────────────────────────────────────────────────
# Antes solo `login()` tenía timeout. Las otras 12 llamadas iban sin ninguno, así
# que un BIMS que acepta la conexión y no responde bloqueaba un worker de
# gunicorn para siempre. Con `--workers 3`, tres de esas y el integrador dejaba
# de atender: es la causa del reinicio cada 6 horas que hay en el cron.
TIMEOUT_CONEXION = 5
# 30 s es 2,4× el request legítimo más lento medido en producción (12,47 s en un
# `create_sale` el 2026-08-24).
TIMEOUT_LECTURA = 30

# Presupuesto total de UNA llamada, reintentos y conmutación de host incluidos.
#
# Un timeout por request no alcanza: 5 intentos de 30 s más la conmutación
# llegarían a ~316 s, y gunicorn corre con `--timeout 120`. Al pasarse, gunicorn
# mata al worker **por señal**, y un worker matado por señal no ejecuta el
# `except` que graba el `FailedOrder`: la orden desaparece sin factura y sin
# rastro. Con 40 s, dos o tres llamadas a BIMS por orden entran cómodas.
#
# Ojo: el presupuesto es por LLAMADA, no por orden. Un presupuesto por orden
# habría que pasarlo desde la vista y toca services.py y views.py; con este
# margen no hace falta.
PRESUPUESTO_REINTENTOS = 40


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


class BimsError(Exception):
    """Error base de comunicación con BIMS."""


class BimsTransientError(BimsError):
    """Error reintentable: red, timeout o status no-ok no terminal."""


class BimsBusinessError(BimsError):
    """
    Rechazo de negocio terminal de BIMS (403, o 401 de permisos persistente
    tras un relogin exitoso). Reintentar no cambia el resultado, así que se
    propaga de inmediato sin agotar los reintentos.
    """


class _BlockAllCookies(cookiejar.DefaultCookiePolicy):
    """
    Política que ni guarda ni envía cookies.

    El cliente usa `requests.Session` solo por el keep-alive; el cookie jar vino
    de arrastre y nunca fue intencional (hasta `81eb9ba` los métodos usaban
    `requests.get/post` pelados, sin jar). Y no es inocuo: BIMS devuelve una
    cookie de sesión y, si se la reenvía junto al header `X-API-Key`, rechaza el
    request con `code: 401` ("Session ID no coincide con la cookie de sesión
    activa"). Como en modo API Key un 401 es terminal, eso cortó la facturación
    en producción el 2026-08-21 al segundo request de cada worker.

    Nuestra autenticación viaja siempre explícita —header `X-API-Key` o `?sid=`—,
    así que no hay nada que una cookie deba sostener.
    """

    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False


class BimsApi:
    def __init__(self) -> None:
        self.base_url = settings.BIMS_URL
        self.primary_url = settings.BIMS_URL
        self.fallback_url = getattr(settings, "BIMS_FALLBACK_URL", None)
        self.ruc_url = getattr(settings, "RUC_URL", None)
        self.api_key = getattr(settings, "BIMS_API_KEY", None)
        self.session = requests.Session()
        self.session.cookies.set_policy(_BlockAllCookies())
        if self.api_key:
            # La key va cruda, sin prefijo de tenant: verificado contra la API
            # viva el 2026-08-21. No hay login ni sesión que expire.
            self.session.headers.update({"X-API-Key": self.api_key})
            self.sid = None
        else:
            # Modo legacy por sesión. BIMS lo corta el 30/09/2026.
            self.sid = self.login()

    def _alternate_base_url(self, url: str) -> Optional[str]:
        """
        Conmuta la instancia a la otra base (primaria ↔ secundaria) y devuelve
        `url` reescrita sobre ella. La conmutación es sticky: las siguientes
        llamadas de la instancia construyen sus URLs sobre la base nueva.
        Devuelve None si no hay BIMS_FALLBACK_URL configurada o si `url` no
        corresponde a ninguna de las dos bases.
        """
        if not self.fallback_url or self.fallback_url == self.primary_url:
            return None
        if url.startswith(self.primary_url):
            old_base, new_base = self.primary_url, self.fallback_url
        elif url.startswith(self.fallback_url):
            old_base, new_base = self.fallback_url, self.primary_url
        else:
            return None
        self.base_url = new_base
        return new_base + url[len(old_base):]

    def login(self) -> Optional[str]:
        url = f"{self.base_url}/users/login/"
        try:
            return self._login_request(url)
        except requests.RequestException:
            alternate_url = self._alternate_base_url(url)
            if alternate_url is None:
                raise
            logging.warning(
                f"Login BIMS sin conexión contra {url}; "
                f"reintentando contra la URL alternativa {alternate_url}"
            )
            bims_logger.warning("══════ BIMS FALLBACK ══════")
            bims_logger.warning(f"Login: conmutando a URL alternativa {alternate_url}")
            return self._login_request(alternate_url)

    def _login_request(self, url: str) -> Optional[str]:
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
            res = requests.post(
                url=url, json=body, timeout=(TIMEOUT_CONEXION, TIMEOUT_LECTURA)
            )
            elapsed = time.time() - start_time

            try:
                response_data = res.json()
            except ValueError:
                bims_logger.error(
                    f"Login: respuesta no es JSON válido. "
                    f"Status: {res.status_code} | Body: {res.text[:500]}"
                )
                raise requests.RequestException(
                    f"BIMS login devolvió respuesta no-JSON (status {res.status_code})"
                )

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
        # Único embudo de las 12 llamadas a BIMS: garantiza que ninguna salga sin
        # timeout. `_retry_loop` ya lo fija recortado al presupuesto restante;
        # este default cubre a quien llame acá directo, sin pasar por el retry.
        kwargs.setdefault("timeout", (TIMEOUT_CONEXION, TIMEOUT_LECTURA))
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

            try:
                response_body = res.json()
            except ValueError:
                bims_logger.error(
                    f"{method_name} {url}: respuesta no es JSON válido. "
                    f"Status: {res.status_code} | Body: {res.text[:500]}"
                )
                raise requests.RequestException(
                    f"BIMS devolvió respuesta no-JSON (status {res.status_code}): {res.text[:200]}"
                )

            # BIMS puede indicar sesión expirada via HTTP 401 o via JSON body code "401" (HTTP 200)
            if res.status_code == 401 or response_body.get("code") == "401":
                if self.api_key:
                    # Con API Key no hay sesión que renovar: el 401 es terminal.
                    raise BimsBusinessError(
                        f"BIMS denegó el acceso a {url} con API Key: "
                        f"{response_body.get('message')} (code 401)"
                    )
                bims_logger.warning(
                    f"Session expired (HTTP {res.status_code} / body code {response_body.get('code')}) "
                    f"| Time: {elapsed:.2f}s | Attempting relogin..."
                )
                logging.info("Session expired, attempting relogin...")
                self.sid = self.login()
                if self.sid:
                    kwargs["params"]["sid"] = self.sid
                    bims_logger.info("══════ BIMS RETRY (after relogin) ══════")
                    bims_logger.info(f"{method_name} {url} | Caller: {caller}")
                    start_time = time.time()
                    res = method(url, **kwargs)
                    elapsed = time.time() - start_time
                    try:
                        response_body = res.json()
                    except ValueError:
                        bims_logger.error(
                            f"{method_name} {url}: respuesta no es JSON válido tras relogin. "
                            f"Status: {res.status_code} | Body: {res.text[:500]}"
                        )
                        raise requests.RequestException(
                            f"BIMS devolvió respuesta no-JSON tras relogin (status {res.status_code}): {res.text[:200]}"
                        )

                    # Si tras un relogin EXITOSO sigue viniendo 401, no es sesión expirada
                    # sino falta de permisos del usuario de API → terminal, no reintentar.
                    if res.status_code == 401 or response_body.get("code") == "401":
                        raise BimsBusinessError(
                            f"BIMS denegó el acceso tras relogin a {url}: "
                            f"{response_body.get('message')} (code 401)"
                        )

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
                # Sin `e.response` (error de red: conexión, timeout, DNS) la única
                # causa real es `str(e)`; sin incluirla acá el mensaje quedaba
                # genérico y esa causa solo sobrevivía en el log, no en la
                # excepción que ve `FailedOrder` (hallazgo 4).
                error_msg += f" Error: {str(e)}"
                bims_logger.error("══════ BIMS ERROR ══════")
                bims_logger.error(f"{method_name} {url} | Time: {elapsed:.2f}s | Error: {str(e)}")
            logging.error(error_msg)
            logging.error(str(e))
            raise BimsTransientError(error_msg) from e


    def _retry_request(self, method, url, max_retries=5, retry_delay=2, **kwargs):
        # Se calcula UNA vez: la conmutación de host comparte este presupuesto en
        # vez de recibir uno nuevo, o el peor caso se duplicaría.
        limite = time.monotonic() + PRESUPUESTO_REINTENTOS
        try:
            return self._retry_loop(method, url, max_retries, retry_delay, limite, **kwargs)
        except BimsTransientError:
            # El chequeo de tiempo va ANTES de `_alternate_base_url`: ese método
            # no es una consulta pura, muta `self.base_url` de forma pegajosa.
            # Llamarlo y después abandonar dejaba la instancia apuntando al host
            # alternativo sin haber mandado un solo request y sin loguearlo.
            #
            # Si el presupuesto de LLAMADA ya se agotó, reentrar al loop con el
            # mismo límite hacía que el segundo loop fallara de inmediato con
            # `Last error: None`, tapando la causa real (hallazgo 4). Y no tiene
            # sentido: no queda tiempo para probar el otro host.
            if time.monotonic() >= limite:
                raise
            alternate_url = self._alternate_base_url(url)
            if alternate_url is None:
                raise
            logging.warning(
                f"BIMS sin respuesta en {url} tras {max_retries} intentos; "
                f"conmutando a la URL alternativa {alternate_url}"
            )
            bims_logger.warning("══════ BIMS FALLBACK ══════")
            bims_logger.warning(f"Conmutando a URL alternativa: {alternate_url}")
            return self._retry_loop(
                method, alternate_url, max_retries, retry_delay, limite, **kwargs
            )

    def _retry_loop(self, method, url, max_retries, retry_delay, limite, **kwargs):
        last_error_details = None
        for attempt in range(1, max_retries + 1):
            restante = limite - time.monotonic()
            # El presupuesto de la ORDEN manda por encima del de esta llamada: es
            # el que gunicorn realmente mide. Se chequea primero porque agotarlo
            # es terminal — no se reintenta ni se conmuta de host.
            restante_orden = deadline.restante()
            if restante_orden is not None and restante_orden <= 0:
                raise deadline.PresupuestoOrdenAgotado(
                    f"Presupuesto de orden de {deadline.PRESUPUESTO_ORDEN}s agotado "
                    f"para {url} tras {attempt - 1} intentos. "
                    f"Last error: {last_error_details}"
                )
            if restante <= 0:
                raise BimsTransientError(
                    f"Presupuesto de {PRESUPUESTO_REINTENTOS}s agotado para {url} "
                    f"tras {attempt - 1} intentos. Last error: {last_error_details}"
                )
            if restante_orden is not None:
                restante = min(restante, restante_orden)
            # Recortar al restante: sin esto el presupuesto sería decorativo,
            # porque un intento que arranca al límite correría entero. Se
            # recorta también la CONEXIÓN (hallazgo 2): dejarla fija en
            # TIMEOUT_CONEXION permitía que un intento se excediera hasta 5 s
            # sobre el presupuesto restante.
            kwargs["timeout"] = (
                min(TIMEOUT_CONEXION, restante),
                min(TIMEOUT_LECTURA, restante),
            )
            try:
                response_data = self._request_with_relogin(method, url, **kwargs)
            except BimsBusinessError:
                # Rechazo terminal (403 / 401 de permisos): propagar sin reintentar.
                raise
            except BimsTransientError as e:
                last_error_details = (
                    f"Error in request to {url}. Attempt {attempt} of {max_retries}. Error: {str(e)}"
                )
                logging.error(last_error_details)
                if attempt < max_retries:
                    time.sleep(retry_delay)
                continue

            if response_data.get("status") == "ok":
                return response_data
            # El check de 403 vive FUERA del try original: nada puede tragarse este corte.
            if response_data.get("code") == "403":
                raise BimsBusinessError(
                    f"BIMS rechazó la solicitud a {url}: "
                    f"{response_data.get('message')} (code 403)"
                )
            # status no-ok no terminal → transitorio, reintentar.
            last_error_details = f"Attempt {attempt} failed: status not 'ok'. Response: {response_data}"
            logging.warning(last_error_details + " Retrying...")
            if attempt < max_retries:
                time.sleep(retry_delay)
        raise BimsTransientError(
            f"Failed request to {url} after {max_retries} attempts. Last error: {last_error_details}"
        )

    def list_contacts(self, document_id: str, document_type: str):
        url = f"{self.base_url}/contacts/"
        params = {
            "document_id": document_id,
            "document_type": document_type,
        }
        if self.sid:
            params["sid"] = self.sid
        response_data = self._retry_request(self.session.get, url, params=params)
        if int(response_data.get("count")) > 0:
            return int(response_data.get("data")[0].get("Contact").get("id"))
        return None

    def find_contact(self, document_id: str, document_type: str) -> Optional[dict]:
        """Busca un contacto y devuelve sus datos completos de BIMS.

        Retorna dict con id, name, document_id, document_type, emails
        o None si no se encuentra.
        """
        url = f"{self.base_url}/contacts/"
        params = {
            "document_id": document_id,
            "document_type": document_type,
        }
        if self.sid:
            params["sid"] = self.sid
        response_data = self._retry_request(self.session.get, url, params=params)
        if int(response_data.get("count")) > 0:
            contact = response_data["data"][0]["Contact"]
            return {
                "id": int(contact["id"]),
                "name": contact.get("name", ""),
                "document_id": contact.get("document_id", ""),
                "document_type": contact.get("document_type", ""),
                "emails": contact.get("emails", ""),
            }
        return None

    ## buscar ruc en turuc
    def find_razon_social_by_ruc(self, document_id: str):
        if not self.ruc_url:
            raise ValueError(
                "RUC_URL no está configurado en settings. "
                "Agregá RUC_URL al .env para usar esta función."
            )
        url = f"{self.ruc_url}/contribuyente/"
        params = {"ruc": document_id}
        # OJO: `requests.get` a propósito, NO `self.session.get`. turuc es un
        # tercero y la sesión lleva el header con el API Key de BIMS.
        response_data = self._retry_request(requests.get, url, params=params)
        if int(response_data.get("count")) > 0:
            return response_data.get("data")[0].get("Contact").get("name")
        return None

    def find_contact_by_ruc(self, document_id: str, document_type: str):
        url = f"{self.base_url}/contacts/"
        params = {
            "document_id": document_id,
            "document_type": document_type,
        }
        if self.sid:
            params["sid"] = self.sid

        response_data = self._retry_request(self.session.get, url, params=params)
        if int(response_data.get("count")) > 0:
            return int(response_data.get("data")[0].get("Contact").get("id"))
        return None

    def find_contact_by_email(self, email: str):
        url = f"{self.base_url}/contacts/"
        params = {
            "email": email,
        }
        if self.sid:
            params["sid"] = self.sid

        response_data = self._retry_request(self.session.get, url, params=params)
        if int(response_data.get("count")) > 0:
            return int(response_data.get("data")[0].get("Contact").get("id"))
        return None

    def find_contact_by_name(self, name: str, email: str = None, document_id: str = None):
        url = f"{self.base_url}/contacts/"
        params = {
            "q": name,
        }
        if self.sid:
            params["sid"] = self.sid

        response_data = self._retry_request(self.session.get, url, params=params)

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
        params = {}
        if self.sid:
            params["sid"] = self.sid
        response_data = self._retry_request(
            self.session.post, url, json=body, params=params
        )
        return response_data.get("data").get("Contact").get("id")

    def update_contact_email(
        self, contact_id: int, name: str,
        document_id: str, document_type: str,
        new_email: str,
    ) -> bool:
        """Intenta actualizar el email de un contacto existente.

        Usa document_id y document_type exactos como están en BIMS
        (obtenidos previamente vía find_contact).
        Retorna True si tuvo éxito, False si falló.
        """
        url = f"{self.base_url}/contacts/"
        body = {
            "Contact": {
                "id": contact_id,
                "name": name,
                "document_id": document_id,
                "document_type": document_type,
                "emails": new_email,
                "company_id": 1,
            }
        }
        params = {}
        if self.sid:
            params["sid"] = self.sid
        try:
            response_data = self._request_with_relogin(
                self.session.post, url, json=body, params=params
            )
            return response_data.get("status") == "ok"
        except Exception as e:
            bims_logger.warning(
                f"No se pudo actualizar email del contacto {contact_id}: {e}"
            )
            return False

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
        params = {}
        if self.sid:
            params["sid"] = self.sid
        response_data = self._retry_request(
            self.session.post, url, json=body, params=params
        )

        data = response_data.get("data")
        if isinstance(data, dict) and data.get("Sale") and data["Sale"].get("id"):
            return data["Sale"]["id"], None
            
        error_msg = response_data.get("message") or response_data.get("error") or "BIMS devolvió HTTP 200 pero no generó la venta (ID vacío)."
        return None, error_msg

    def send_invoice(self, sale_id):
        url = f"{self.base_url}/sales/send/{sale_id}/"
        params = {}
        if self.sid:
            params["sid"] = self.sid
        response_data = self._retry_request(self.session.get, url, params=params)
        return "ok" if response_data.get("status") == "ok" else None

    def get_posales(self):
        """
        Puntos de venta de BIMS, como lista de `(id, nombre)` ordenada por id.

        BIMS es dueño del `posale_id`: WooCommerce y FooEvents no lo conocen.
        Esto permite que el admin ofrezca la lista real en vez de dejar escribir
        un número que recién falla al facturar.

        Verificado contra la API viva el 2026-08-24: responde `/posales/` (sin
        sufijo `.json`) con la convención CakePHP `data: [{"Posale": {...}}]`, y
        **los ids vienen como strings** — se convierten a int para que casen con
        `Sucursal.bims_posale_id`. Devolvió 4: 1 Caja Tatakualab, 4 Caja San
        Cosmos, 6 Caja WEB, 7 Caja Fund MuCi.
        """
        url = f"{self.base_url}/posales/"
        params = {}
        if self.sid:
            params["sid"] = self.sid
        response_data = self._retry_request(self.session.get, url, params=params)

        posales = []
        for fila in response_data.get("data") or []:
            posale = fila.get("Posale") or {}
            if posale.get("id") is None:
                continue
            posales.append((int(posale["id"]), posale.get("name") or "(sin nombre)"))
        return sorted(posales)

    def get_contacts(self, limit=500, offset=0):
        url = f"{self.base_url}/contacts/"
        params = {
            "limit": limit,
            "offset": offset,
        }
        if self.sid:
            params["sid"] = self.sid
        return self._retry_request(self.session.get, url, params=params)


bims = BimsApi()
