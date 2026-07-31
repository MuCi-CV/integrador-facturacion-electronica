import requests
from django.db import IntegrityError
from unittest.mock import MagicMock, patch

# bims.py instancia BimsApi() al ser importado, lo que intenta conectar a BIMS.
# Mockeamos requests.post antes de importar core.services para evitar esa conexión.
with patch("requests.post") as _mock_post:
    _mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"status": "ok", "data": {"Session": {"id": "mock_sid"}}},
    )
    from core.services import _parse_pos_payments, build_sale_products, resolve_pos_and_payments
    from core.bims import BimsApi, BimsBusinessError, BimsTransientError

from core.models import RucCache
from django.test import TestCase, override_settings


class ParsePosPaymentsTest(TestCase):
    """Tests para la lógica de parseo de métodos de pago de FooEvents POS."""

    def _meta(self, value: str) -> list:
        return [{"key": "_fooeventspos_payments", "value": value}]

    def test_single_payment_amount_zero_usa_total(self):
        """
        Regresión: FooEvents POS (versiones recientes) envía "amount": 0 para pagos únicos.
        El sistema debe usar el total de la orden en lugar del 0.
        """
        meta_data = self._meta('[{"opmk": "fooeventspos_other", "amount": 0}]')
        result = _parse_pos_payments(meta_data, total=40000)
        self.assertEqual(result, [{"payment_method_id": 27, "amount": 40000}])

    def test_single_payment_sin_campo_amount_usa_total(self):
        """Comportamiento original (FooEvents anterior): sin campo 'amount'."""
        meta_data = self._meta('[{"opmk": "fooeventspos_cash"}]')
        result = _parse_pos_payments(meta_data, total=25000)
        self.assertEqual(result, [{"payment_method_id": 21, "amount": 25000}])

    def test_multiples_pagos_usan_montos_individuales(self):
        """Pagos mixtos (ej. efectivo + Bancard): cada método tiene su monto real."""
        meta_data = self._meta(
            '[{"opmk": "fooeventspos_cash", "amount": 5000},'
            ' {"opmk": "fooeventspos_other", "amount": 65000}]'
        )
        result = _parse_pos_payments(meta_data, total=70000)
        self.assertEqual(result, [
            {"payment_method_id": 21, "amount": 5000.0},
            {"payment_method_id": 27, "amount": 65000.0},
        ])

    def test_metodo_desconocido_usa_default_online(self):
        meta_data = self._meta('[{"opmk": "fooeventspos_unknown", "amount": 0}]')
        result = _parse_pos_payments(meta_data, total=10000)
        self.assertEqual(result, [{"payment_method_id": 28, "amount": 10000}])

    def test_sin_metadata_retorna_lista_vacia(self):
        result = _parse_pos_payments([], total=10000)
        self.assertEqual(result, [])

    def test_mapeo_todos_los_metodos_conocidos(self):
        metodos = {
            "fooeventspos_check_payment": 34,
            "fooeventspos_cash": 21,
            "fooeventspos_cash_on_delivery": 26,
            "fooeventspos_other": 27,
            "fooeventspos_online": 28,
        }
        for opmk, expected_id in metodos.items():
            with self.subTest(opmk=opmk):
                meta_data = self._meta(f'[{{"opmk": "{opmk}", "amount": 0}}]')
                result = _parse_pos_payments(meta_data, total=10000)
                self.assertEqual(result[0]["payment_method_id"], expected_id)
                self.assertEqual(result[0]["amount"], 10000)


class ResolvePosAndPaymentsTest(TestCase):
    """Tests para la resolución de punto de venta y métodos de pago."""

    def test_orden_web_retorna_posale_6_y_pago_online(self):
        result = resolve_pos_and_payments([], total=30000, payment_method_title="Credit Card")
        self.assertEqual(result, (6, [{"payment_method_id": 28, "amount": 30000}]))

    def test_cuenta_administrador_retorna_none(self):
        meta_data = [{"key": "_fooeventspos_user_id", "value": "2"}]
        result = resolve_pos_and_payments(meta_data, total=30000, payment_method_title="Cash")
        self.assertIsNone(result)

    def test_cortesia_retorna_none(self):
        meta_data = [{"key": "_fooeventspos_user_id", "value": "729"}]
        result = resolve_pos_and_payments(meta_data, total=30000, payment_method_title="Cortesía")
        self.assertIsNone(result)

    def test_san_cosmos_mapea_a_posale_4(self):
        meta_data = [
            {"key": "_fooeventspos_user_id", "value": "729"},
            {"key": "_fooeventspos_payments", "value": '[{"opmk": "fooeventspos_cash", "amount": 0}]'},
        ]
        posale_id, _ = resolve_pos_and_payments(meta_data, total=30000, payment_method_title="Cash")
        self.assertEqual(posale_id, 4)

    def test_tatakualab_mapea_a_posale_1(self):
        meta_data = [
            {"key": "_fooeventspos_user_id", "value": "3"},
            {"key": "_fooeventspos_payments", "value": '[{"opmk": "fooeventspos_cash", "amount": 0}]'},
        ]
        posale_id, _ = resolve_pos_and_payments(meta_data, total=20000, payment_method_title="Cash")
        self.assertEqual(posale_id, 1)

    def test_cajero_desconocido_mapea_a_posale_7(self):
        meta_data = [
            {"key": "_fooeventspos_user_id", "value": "999"},
            {"key": "_fooeventspos_payments", "value": '[{"opmk": "fooeventspos_cash", "amount": 0}]'},
        ]
        posale_id, _ = resolve_pos_and_payments(meta_data, total=15000, payment_method_title="Cash")
        self.assertEqual(posale_id, 7)


class BuildSaleProductsTest(TestCase):
    """Tests para la construcción del payload de productos."""

    def _make_item(self, product_id: int, quantity: int, total: str, total_tax: str, name: str = "Producto") -> dict:
        return {
            "product_id": product_id,
            "variation_id": 0,
            "quantity": quantity,
            "total": total,
            "total_tax": total_tax,
            "name": name,
        }

    @patch("core.services.wc_api")
    def test_producto_sin_sku_es_omitido(self, mock_wc):
        mock_wc.get_product.return_value = {"sku": ""}
        items = [self._make_item(123, 1, "10000", "1000")]
        products, skipped = build_sale_products(1, items, [], discount=0)
        self.assertEqual(products, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("sin SKU", skipped[0])

    @patch("core.services.wc_api")
    def test_precio_estandar_incluye_impuesto(self, mock_wc):
        mock_wc.get_product.return_value = {"sku": "500"}
        items = [self._make_item(1, 2, "20000", "2000")]
        products, skipped = build_sale_products(1, items, [], discount=0)
        self.assertEqual(skipped, [])
        self.assertEqual(products, [{"product_id": 500, "quantity": 2, "price": 11000.0}])

    @patch("core.services.wc_api")
    def test_tip_se_agrega_como_producto_100(self, mock_wc):
        mock_wc.get_product.return_value = {"sku": "500"}
        items = [self._make_item(1, 1, "10000", "1000")]
        fee_lines = [{"name": "Tip", "total": "5000"}]
        products, _ = build_sale_products(1, items, fee_lines, discount=0)
        tip = next((p for p in products if p["product_id"] == 100), None)
        self.assertIsNotNone(tip)
        self.assertEqual(tip["price"], 5000.0)
        self.assertEqual(tip["quantity"], 1.0)

    @patch("core.services.wc_api")
    def test_item_con_cantidad_cero_es_omitido(self, mock_wc):
        mock_wc.get_product.return_value = {"sku": "500"}
        items = [self._make_item(1, 0, "0", "0")]
        products, skipped = build_sale_products(1, items, [], discount=0)
        self.assertEqual(products, [])
        self.assertEqual(len(skipped), 1)


@patch("core.bims.time.sleep")  # evita esperas reales durante los reintentos
class RetryRequestTest(TestCase):
    """Tests del control de reintentos vs. fallo terminal en _retry_request."""

    def setUp(self):
        with patch.object(BimsApi, "login", return_value="fake_sid"):
            self.api = BimsApi()

    def test_403_falla_de_inmediato_sin_reintentar(self, mock_sleep):
        """Bug A: un 403 es un rechazo de negocio terminal, no debe agotar reintentos."""
        response = {"status": "error", "code": "403", "message": "No tienes acceso a este recurso"}
        with patch.object(self.api, "_request_with_relogin", return_value=response) as mock_req:
            with self.assertRaises(BimsBusinessError) as ctx:
                self.api._retry_request(requests.post, "http://bims.test/contacts/")
        self.assertEqual(mock_req.call_count, 1)
        self.assertIn("403", str(ctx.exception))
        mock_sleep.assert_not_called()

    def test_error_de_red_reintenta_hasta_max_retries(self, mock_sleep):
        """Un error transitorio (red) sí debe reintentarse el número de veces configurado."""
        with patch.object(
            self.api, "_request_with_relogin", side_effect=BimsTransientError("network down")
        ) as mock_req:
            with self.assertRaises(BimsTransientError):
                self.api._retry_request(requests.get, "http://bims.test/contacts/", max_retries=3)
        self.assertEqual(mock_req.call_count, 3)

    def test_status_ok_retorna_sin_reintentar(self, mock_sleep):
        response = {"status": "ok", "data": {"Contact": {"id": 1}}}
        with patch.object(self.api, "_request_with_relogin", return_value=response) as mock_req:
            result = self.api._retry_request(requests.get, "http://bims.test/contacts/")
        self.assertEqual(result, response)
        self.assertEqual(mock_req.call_count, 1)

    def test_status_no_ok_no_terminal_reintenta(self, mock_sleep):
        """Un status no-ok que no sea 403/401 se trata como transitorio y se reintenta."""
        response = {"status": "error", "code": "500", "message": "boom"}
        with patch.object(self.api, "_request_with_relogin", return_value=response) as mock_req:
            with self.assertRaises(BimsTransientError):
                self.api._retry_request(requests.get, "http://bims.test/x/", max_retries=4)
        self.assertEqual(mock_req.call_count, 4)

    def test_no_duerme_tras_el_ultimo_intento(self, mock_sleep):
        """No debe hacer sleep después del intento final (latencia desperdiciada en webhook síncrono)."""
        with patch.object(
            self.api, "_request_with_relogin", side_effect=BimsTransientError("network down")
        ):
            with self.assertRaises(BimsTransientError):
                self.api._retry_request(requests.get, "http://bims.test/x/", max_retries=3)
        # 3 intentos => como mucho 2 esperas entre ellos, nunca 3.
        self.assertEqual(mock_sleep.call_count, 2)

    def test_401_de_permisos_persistente_tras_relogin_es_terminal(self, mock_sleep):
        """
        Tras un relogin EXITOSO, si BIMS sigue devolviendo code 401 es un problema de
        permisos (no de sesión expirada) y debe fallar como terminal, sin reintentar.
        """
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "status": "error",
            "code": "401",
            "message": "No dispone de permisos para acceder a la función solicitada.",
        }
        resp.raise_for_status.return_value = None
        method = MagicMock(return_value=resp)
        method.__name__ = "get"  # el código real lee method.__name__ para loguear
        with patch.object(self.api, "login", return_value="new_sid"):
            with self.assertRaises(BimsBusinessError):
                self.api._request_with_relogin(
                    method, "http://bims.test/contacts/", params={"sid": "old_sid"}
                )


@patch("core.bims.time.sleep")  # evita esperas reales durante los reintentos
class BimsFallbackUrlTest(TestCase):
    """
    Tests de la conmutación a la URL secundaria (BIMS_FALLBACK_URL) cuando la
    base en uso agota sus reintentos por errores transitorios.
    """

    FALLBACK = "http://in.bims.test.local"

    def setUp(self):
        with patch.object(BimsApi, "login", return_value="fake_sid"):
            self.api = BimsApi()
        self.api.fallback_url = self.FALLBACK

    def test_agotada_la_primaria_conmuta_a_la_secundaria(self, mock_sleep):
        ok = {"status": "ok", "data": {}}
        with patch.object(
            self.api,
            "_request_with_relogin",
            side_effect=[BimsTransientError("down")] * 3 + [ok],
        ) as mock_req:
            result = self.api._retry_request(
                requests.get, f"{self.api.primary_url}/contacts/", max_retries=3
            )
        self.assertEqual(result, ok)
        self.assertEqual(mock_req.call_count, 4)
        # El intento exitoso fue contra la URL secundaria.
        self.assertEqual(mock_req.call_args[0][1], f"{self.FALLBACK}/contacts/")

    def test_la_conmutacion_es_sticky_para_la_instancia(self, mock_sleep):
        ok = {"status": "ok", "data": {}}
        with patch.object(
            self.api,
            "_request_with_relogin",
            side_effect=[BimsTransientError("down")] * 3 + [ok],
        ):
            self.api._retry_request(
                requests.get, f"{self.api.primary_url}/contacts/", max_retries=3
            )
        # Las próximas URLs de la instancia se construyen sobre la secundaria.
        self.assertEqual(self.api.base_url, self.FALLBACK)

    def test_desde_la_secundaria_puede_volver_a_la_primaria(self, mock_sleep):
        """La conmutación es simétrica: si la secundaria falla, reintenta la primaria."""
        self.api.base_url = self.FALLBACK
        ok = {"status": "ok", "data": {}}
        with patch.object(
            self.api,
            "_request_with_relogin",
            side_effect=[BimsTransientError("down")] * 3 + [ok],
        ) as mock_req:
            self.api._retry_request(
                requests.get, f"{self.FALLBACK}/contacts/", max_retries=3
            )
        self.assertEqual(mock_req.call_args[0][1], f"{self.api.primary_url}/contacts/")
        self.assertEqual(self.api.base_url, self.api.primary_url)

    def test_sin_secundaria_configurada_mantiene_comportamiento_actual(self, mock_sleep):
        self.api.fallback_url = None
        with patch.object(
            self.api, "_request_with_relogin", side_effect=BimsTransientError("down")
        ) as mock_req:
            with self.assertRaises(BimsTransientError):
                self.api._retry_request(
                    requests.get, f"{self.api.primary_url}/x/", max_retries=3
                )
        self.assertEqual(mock_req.call_count, 3)

    def test_si_la_secundaria_tambien_falla_propaga_transitorio(self, mock_sleep):
        with patch.object(
            self.api, "_request_with_relogin", side_effect=BimsTransientError("down")
        ) as mock_req:
            with self.assertRaises(BimsTransientError):
                self.api._retry_request(
                    requests.get, f"{self.api.primary_url}/x/", max_retries=3
                )
        # 3 intentos contra la primaria + 3 contra la secundaria.
        self.assertEqual(mock_req.call_count, 6)

    def test_error_terminal_no_conmuta(self, mock_sleep):
        """Un 403 es rechazo de negocio: cambiar de URL no cambia el resultado."""
        response = {"status": "error", "code": "403", "message": "sin acceso"}
        with patch.object(
            self.api, "_request_with_relogin", return_value=response
        ) as mock_req:
            with self.assertRaises(BimsBusinessError):
                self.api._retry_request(
                    requests.get, f"{self.api.primary_url}/contacts/"
                )
        self.assertEqual(mock_req.call_count, 1)
        self.assertEqual(self.api.base_url, self.api.primary_url)

    def test_login_conmuta_a_la_secundaria_si_no_hay_conexion(self, mock_sleep):
        ok = MagicMock(status_code=200)
        ok.json.return_value = {
            "status": "ok",
            "data": {"Session": {"id": "sid-fallback"}},
        }
        with patch(
            "core.bims.requests.post",
            side_effect=[requests.ConnectionError("primaria caída"), ok],
        ) as mock_post:
            sid = self.api.login()
        self.assertEqual(sid, "sid-fallback")
        self.assertEqual(self.api.base_url, self.FALLBACK)
        self.assertEqual(
            mock_post.call_args[1]["url"], f"{self.FALLBACK}/users/login/"
        )

    def test_login_sin_secundaria_propaga_el_error(self, mock_sleep):
        self.api.fallback_url = None
        with patch(
            "core.bims.requests.post", side_effect=requests.ConnectionError("down")
        ):
            with self.assertRaises(requests.RequestException):
                self.api.login()


class GetRazonSocialTest(TestCase):
    def _make_cache(self, ruc, razon, dias_atras):
        from django.utils.timezone import now
        from datetime import timedelta

        return RucCache.objects.create(
            ruc=ruc, razon_social=razon, checked_at=now() - timedelta(days=dias_atras)
        )

    @patch("core.ruc._fetch_from_api")
    def test_cache_fresco_no_llama_api(self, mock_fetch):
        from core.ruc import get_razon_social

        self._make_cache("80012345-6", "EMPRESA FRESCA SA", dias_atras=5)
        self.assertEqual(get_razon_social("80012345-6"), "EMPRESA FRESCA SA")
        mock_fetch.assert_not_called()

    @patch("core.ruc._fetch_from_api")
    def test_cache_vencido_api_ok_actualiza(self, mock_fetch):
        from core.ruc import get_razon_social
        from django.utils.timezone import now
        from datetime import timedelta

        self._make_cache("80012345-6", "NOMBRE VIEJO SA", dias_atras=40)
        mock_fetch.return_value = "NOMBRE NUEVO SA"

        self.assertEqual(get_razon_social("80012345-6"), "NOMBRE NUEVO SA")
        row = RucCache.objects.get(ruc="80012345-6")
        self.assertEqual(row.razon_social, "NOMBRE NUEVO SA")
        self.assertGreater(row.checked_at, now() - timedelta(minutes=1))

    @patch("core.ruc._fetch_from_api")
    def test_cache_vencido_api_falla_usa_viejo_sin_renovar(self, mock_fetch):
        from core.ruc import get_razon_social

        row = self._make_cache("80012345-6", "NOMBRE VIEJO SA", dias_atras=40)
        viejo_checked_at = row.checked_at
        mock_fetch.return_value = None

        self.assertEqual(get_razon_social("80012345-6"), "NOMBRE VIEJO SA")
        self.assertEqual(RucCache.objects.get(ruc="80012345-6").checked_at, viejo_checked_at)

    @patch("core.ruc._fetch_from_api")
    def test_sin_cache_api_ok_crea_fila(self, mock_fetch):
        from core.ruc import get_razon_social

        mock_fetch.return_value = "EMPRESA NUEVA SA"
        self.assertEqual(get_razon_social("80012345-6"), "EMPRESA NUEVA SA")
        self.assertTrue(RucCache.objects.filter(ruc="80012345-6").exists())

    @patch("core.ruc._fetch_from_api")
    def test_sin_cache_api_falla_devuelve_none(self, mock_fetch):
        from core.ruc import get_razon_social

        mock_fetch.return_value = None
        self.assertIsNone(get_razon_social("80012345-6"))
        self.assertFalse(RucCache.objects.filter(ruc="80012345-6").exists())

    @patch("core.ruc.RucCache.objects.update_or_create")
    @patch("core.ruc._fetch_from_api")
    def test_integrity_error_en_cache_no_propaga(self, mock_fetch, mock_uoc):
        """Regresión: un IntegrityError por insert concurrente no debe propagarse al caller."""
        from core.ruc import get_razon_social

        mock_fetch.return_value = "EMPRESA CONCURRENTE SA"
        mock_uoc.side_effect = IntegrityError("dup")

        result = get_razon_social("80012345-6")

        self.assertEqual(result, "EMPRESA CONCURRENTE SA")


class RucCacheModelTest(TestCase):
    def test_se_crea_y_es_unico_por_ruc(self):
        from django.utils.timezone import now
        from django.db import IntegrityError

        RucCache.objects.create(
            ruc="80012345-6", razon_social="EMPRESA SA", checked_at=now()
        )
        self.assertEqual(RucCache.objects.get(ruc="80012345-6").razon_social, "EMPRESA SA")
        with self.assertRaises(IntegrityError):
            RucCache.objects.create(
                ruc="80012345-6", razon_social="OTRA SA", checked_at=now()
            )


class FetchFromApiTest(TestCase):
    def _resp(self, json_data, status=200):
        m = MagicMock(status_code=status)
        m.json.return_value = json_data
        m.raise_for_status.return_value = None
        return m

    @patch("core.ruc.requests.get")
    def test_positivo_devuelve_razon_social(self, mock_get):
        from core.ruc import _fetch_from_api

        mock_get.return_value = self._resp(
            {"data": {"razonSocial": "COMERCIO Y FINANZAS SA"}, "message": "OK"}
        )
        self.assertEqual(_fetch_from_api("80012345-6"), "COMERCIO Y FINANZAS SA")

    @patch("core.ruc.requests.get")
    def test_sin_match_devuelve_none(self, mock_get):
        from core.ruc import _fetch_from_api

        mock_get.return_value = self._resp({"data": {}, "message": "OK"})
        self.assertIsNone(_fetch_from_api("80012345-6"))

    @patch("core.ruc.requests.get")
    def test_error_de_red_devuelve_none(self, mock_get):
        from core.ruc import _fetch_from_api

        mock_get.side_effect = requests.RequestException("timeout")
        self.assertIsNone(_fetch_from_api("80012345-6"))

    @override_settings(RUC_API_URL=None)
    @patch("core.ruc.requests.get")
    def test_no_configurado_no_hace_request(self, mock_get):
        from core.ruc import _fetch_from_api

        self.assertIsNone(_fetch_from_api("80012345-6"))
        mock_get.assert_not_called()

    @patch("core.ruc.requests.get")
    def test_json_malformado_devuelve_none(self, mock_get):
        from core.ruc import _fetch_from_api

        m = MagicMock(status_code=200)
        m.json.side_effect = ValueError("no JSON")
        m.raise_for_status.return_value = None
        mock_get.return_value = m
        self.assertIsNone(_fetch_from_api("80012345-6"))


class ResolveContactRucEnrichmentTest(TestCase):
    def _meta(self, ruc, razon_social):
        return [
            {"key": "_billing_ruc", "value": ruc},
            {"key": "_billing_razon_social", "value": razon_social},
        ]

    @patch("core.services.bims")
    @patch("core.services.get_razon_social")
    def test_ruc_con_fuente_positiva_usa_nombre_autoritativo(self, mock_ruc, mock_bims):
        from core.services import resolve_contact_id

        mock_ruc.return_value = "RAZON SOCIAL OFICIAL SA"
        mock_bims.find_contact.return_value = None
        mock_bims.create_contact.return_value = 999

        resolve_contact_id(
            order_id=1,
            meta_data=self._meta("80012345-6", "nombre mal escrito"),
            billing={"first_name": "Juan", "last_name": "Pérez", "email": "j@x.com"},
            shipping={},
            is_pos=False,
        )

        mock_ruc.assert_called_once_with("80012345-6")
        # el name autoritativo llega a create_contact
        _, kwargs = mock_bims.create_contact.call_args
        self.assertEqual(kwargs["name"], "RAZON SOCIAL OFICIAL SA")

    @patch("core.services.bims")
    @patch("core.services.get_razon_social")
    def test_ci_no_consulta_la_fuente(self, mock_ruc, mock_bims):
        from core.services import resolve_contact_id

        mock_bims.find_contact.return_value = None
        mock_bims.create_contact.return_value = 999

        resolve_contact_id(
            order_id=2,
            meta_data=[{"key": "_billing_documento", "value": "1234567"}],
            billing={"first_name": "Ana", "last_name": "López", "email": "a@x.com"},
            shipping={},
            is_pos=False,
        )

        mock_ruc.assert_not_called()


from core.models import ContactCache
from core import contact_lookup


class LookupContactTest(TestCase):
    def test_normalize_document_con_verificador(self):
        base, clean = contact_lookup._normalize_document("2109835-2")
        self.assertEqual(base, "2109835")
        self.assertEqual(clean, "2109835-2")

    def test_normalize_document_sin_verificador(self):
        base, clean = contact_lookup._normalize_document("2109835")
        self.assertEqual(base, "2109835")
        self.assertEqual(clean, "2109835")

    @patch("core.contact_lookup.get_razon_social")
    def test_por_ruc_con_contacto_y_razon(self, mock_razon):
        mock_razon.return_value = "Carlos Vallory"
        ContactCache.objects.create(bims_id=1, email="carlos@muci.org", document_id="2109835")
        result = contact_lookup.lookup_contact(ruc="2109835-2")
        self.assertEqual(result["email"], "carlos@muci.org")
        self.assertEqual(result["razon_social"], "Carlos Vallory")
        self.assertEqual(result["documento"], "2109835")
        self.assertEqual(result["ruc"], "2109835-2")
        self.assertEqual(result["source"], "contactcache")

    @patch("core.contact_lookup.get_razon_social")
    def test_por_ruc_sin_contacto_solo_razon(self, mock_razon):
        mock_razon.return_value = "Empresa SA"
        result = contact_lookup.lookup_contact(ruc="80012345-6")
        self.assertIsNone(result["email"])
        self.assertEqual(result["razon_social"], "Empresa SA")
        self.assertEqual(result["source"], "ruc")

    @patch("core.contact_lookup.get_razon_social")
    def test_por_ruc_sin_nada(self, mock_razon):
        mock_razon.return_value = None
        result = contact_lookup.lookup_contact(ruc="99999999-9")
        self.assertIsNone(result["razon_social"])
        self.assertIsNone(result["email"])
        self.assertEqual(result["documento"], "99999999")
        self.assertEqual(result["source"], "none")

    @patch("core.contact_lookup.get_razon_social")
    def test_por_email_con_contacto(self, mock_razon):
        mock_razon.return_value = "Carlos Vallory"
        ContactCache.objects.create(bims_id=2, email="carlos@muci.org", document_id="2109835")
        result = contact_lookup.lookup_contact(email="carlos@muci.org")
        self.assertEqual(result["email"], "carlos@muci.org")
        self.assertEqual(result["documento"], "2109835")
        self.assertEqual(result["razon_social"], "Carlos Vallory")
        self.assertEqual(result["source"], "contactcache")

    def test_por_email_sin_contacto(self):
        result = contact_lookup.lookup_contact(email="nadie@muci.org")
        self.assertEqual(result["email"], "nadie@muci.org")
        self.assertEqual(result["source"], "none")

    def test_sin_argumentos(self):
        result = contact_lookup.lookup_contact()
        self.assertEqual(result["source"], "none")


from unittest.mock import patch as _patch

from rest_framework.test import APIRequestFactory

from core.lookup_views import ContactLookupView


class ContactLookupViewTest(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = ContactLookupView.as_view()

    def test_sin_token_devuelve_401(self):
        request = self.factory.get("/contact-lookup/", {"ruc": "2109835-2"})
        response = self.view(request)
        self.assertEqual(response.status_code, 401)

    def test_token_incorrecto_devuelve_401(self):
        request = self.factory.get(
            "/contact-lookup/", {"ruc": "2109835-2"}, HTTP_X_MUCI_TOKEN="mal"
        )
        response = self.view(request)
        self.assertEqual(response.status_code, 401)

    def test_sin_params_devuelve_400(self):
        request = self.factory.get("/contact-lookup/", HTTP_X_MUCI_TOKEN="test-token")
        response = self.view(request)
        self.assertEqual(response.status_code, 400)

    @_patch("core.lookup_views.lookup_contact")
    def test_ok_devuelve_datos(self, mock_lookup):
        mock_lookup.return_value = {
            "razon_social": "Carlos Vallory",
            "email": "carlos@muci.org",
            "documento": "2109835",
            "ruc": "2109835-2",
            "source": "contactcache",
        }
        request = self.factory.get(
            "/contact-lookup/", {"ruc": "2109835-2"}, HTTP_X_MUCI_TOKEN="test-token"
        )
        response = self.view(request)
        self.assertEqual(response.status_code, 200)
        # La vista devuelve el dict tal cual en snake_case; el camelCasing de las claves
        # es responsabilidad del CamelCaseJSONRenderer de PRODUCCIÓN (settings.py), que
        # test_settings.py no configura. Por eso verificamos response.data, no el render.
        self.assertEqual(response.data["razon_social"], "Carlos Vallory")
        self.assertEqual(response.data["source"], "contactcache")
        mock_lookup.assert_called_once_with(ruc="2109835-2", email=None)


class ResolveContactPosNameTest(TestCase):
    """El nombre del contacto en órdenes POS debe incluir nombre y apellido."""

    @patch("core.services.bims")
    @patch("core.services.get_razon_social")
    def test_pos_usa_nombre_y_apellido_de_billing(self, mock_ruc, mock_bims):
        from core.services import resolve_contact_id

        mock_bims.find_contact.return_value = None
        mock_bims.create_contact.return_value = 999

        # FooEvents POS envía nombre y apellido separados en billing, y espeja
        # el apellido en shipping.last_name. El integrador no debe quedarse solo
        # con el apellido.
        resolve_contact_id(
            order_id=1,
            meta_data=[],
            billing={"first_name": "Grecia", "last_name": "Barreto", "email": "g@x.com"},
            shipping={"company": "5292120-4", "last_name": "Barreto"},
            is_pos=True,
        )

        _, kwargs = mock_bims.create_contact.call_args
        self.assertEqual(kwargs["name"], "Grecia Barreto")
