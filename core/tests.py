import email
import json
import time
import requests
from django.db import IntegrityError, transaction
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# bims.py instancia BimsApi() al ser importado, lo que intenta conectar a BIMS.
# Mockeamos requests.post antes de importar core.services para evitar esa conexión.
with patch("requests.post") as _mock_post:
    _mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"status": "ok", "data": {"Session": {"id": "mock_sid"}}},
    )
    from core.services import (
        _parse_pos_payments,
        build_sale_products,
        process_order,
        resolve_pos_and_payments,
    )
    from core.bims import (
        BimsApi,
        BimsBusinessError,
        BimsError,
        BimsTransientError,
        PRESUPUESTO_REINTENTOS,
        REDACTADO,
        TIMEOUT_CONEXION,
        TIMEOUT_LECTURA,
        _redactar,
        _redactar_texto,
    )

from core.constants import (
    FLAT_PRICE_PRODUCT_IDS,
    POS_DEFAULT_POSALE_ID,
    POS_USER_ID_TO_POSALE,
    WEB_POSALE_ID,
)
from core.models import FailedOrder, RucCache, Sucursal
from core.forms import SucursalForm
from core.woocommerce import (
    TIMEOUT_CONEXION_WOOCOMMERCE,
    TIMEOUT_WOOCOMMERCE,
    WooCommerceAPI,
)
from core.sucursales import completar_desde_woocommerce, opciones_de_punto_de_venta
from core import deadline
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

    def test_cortesia_se_factura_con_el_metodo_de_pago_de_bims(self):
        """
        Antes se descartaba la orden entera. Finanzas la quiere facturada al precio
        original, con la cortesía rastreable: el método de pago 43 de BIMS.
        """
        meta_data = [
            {"key": "_fooeventspos_user_id", "value": "729"},
            {
                "key": "_fooeventspos_payments",
                "value": '[{"opmk": "fooeventspos_direct_bank_transfer", "amount": 0}]',
            },
        ]
        result = resolve_pos_and_payments(meta_data, total=200000, payment_method_title="Cortesía")
        self.assertEqual(result, (4, [{"payment_method_id": 43, "amount": 200000}]))

    def test_opmk_desconocido_avisa_antes_de_caer_al_fallback(self):
        """
        Un `opmk` que no está en el mapeo se factura como "En línea" (28). Eso es
        exactamente el bug que tuvo Cortesía durante 3 años: FooEvents reetiqueta un
        slot y nadie se entera. El fallback se queda, pero deja de ser silencioso.
        """
        meta_data = [
            {"key": "_fooeventspos_user_id", "value": "729"},
            {
                "key": "_fooeventspos_payments",
                "value": '[{"opmk": "fooeventspos_slot_nuevo", "amount": 0}]',
            },
        ]
        with self.assertLogs("core.services", level="WARNING") as registro:
            _, pagos = resolve_pos_and_payments(
                meta_data, total=30000, payment_method_title="Algo Nuevo"
            )

        self.assertEqual(pagos, [{"payment_method_id": 28, "amount": 30000}])
        self.assertIn("fooeventspos_slot_nuevo", "\n".join(registro.output))

    def test_transferencia_directa_no_se_confunde_con_cortesia(self):
        """
        FooEvents usa dos slots distintos: `direct_bank_transfer` está etiquetado
        "Cortesía" y `cash_on_delivery` es la transferencia bancaria de verdad (26).
        Confundirlos haría figurar una cortesía como cobrada.
        """
        meta_data = [
            {"key": "_fooeventspos_user_id", "value": "729"},
            {
                "key": "_fooeventspos_payments",
                "value": '[{"opmk": "fooeventspos_cash_on_delivery", "amount": 0}]',
            },
        ]
        _, pagos = resolve_pos_and_payments(
            meta_data, total=30000, payment_method_title="Transferencia Bancaria directa"
        )
        self.assertEqual(pagos, [{"payment_method_id": 26, "amount": 30000}])

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

    @patch("core.services.wc_api")
    def test_item_con_precio_cero_es_omitido(self, mock_wc):
        mock_wc.get_product.return_value = {"sku": "500"}
        items = [self._make_item(1, 1, "0", "0")]
        products, skipped = build_sale_products(1, items, [], discount=0)
        self.assertEqual(products, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("precio 0", skipped[0])

    @patch("core.services.wc_api")
    def test_item_con_precio_cero_no_afecta_a_los_demas(self, mock_wc):
        mock_wc.get_product.side_effect = [{"sku": "500"}, {"sku": "600"}]
        items = [
            self._make_item(1, 1, "0", "0", name="Gratis"),
            self._make_item(2, 2, "20000", "2000", name="Pago"),
        ]
        products, skipped = build_sale_products(1, items, [], discount=0)
        self.assertEqual(products, [{"product_id": 600, "quantity": 2, "price": 11000.0}])
        self.assertEqual(len(skipped), 1)

    @patch("core.services.wc_api")
    def test_precio_unitario_que_redondea_a_cero_es_omitido(self, mock_wc):
        # 1 Gs. repartido en 300 unidades redondea a 0.0: llegaría a BIMS con precio 0.
        mock_wc.get_product.return_value = {"sku": "500"}
        items = [self._make_item(1, 300, "1", "0")]
        products, skipped = build_sale_products(1, items, [], discount=0)
        self.assertEqual(products, [])
        self.assertEqual(len(skipped), 1)

    @patch("core.services.wc_api")
    def test_item_flat_price_con_total_cero_es_omitido(self, mock_wc):
        # La rama flat envía item["total"] sin impuesto: con total 0 el precio sería 0
        # aunque total_with_tax sea > 0.
        mock_wc.get_product.return_value = {"sku": "500"}
        flat_id = next(iter(FLAT_PRICE_PRODUCT_IDS))
        items = [self._make_item(flat_id, 1, "0", "1000")]
        products, skipped = build_sale_products(1, items, [], discount=0)
        self.assertEqual(products, [])
        self.assertEqual(len(skipped), 1)

    @patch("core.services.wc_api")
    def test_tip_en_cero_es_omitido(self, mock_wc):
        mock_wc.get_product.return_value = {"sku": "500"}
        items = [self._make_item(1, 1, "10000", "1000")]
        fee_lines = [{"name": "Tip", "total": "0"}]
        products, _ = build_sale_products(1, items, fee_lines, discount=0)
        self.assertEqual([p for p in products if p["product_id"] == 100], [])

    @patch("core.services.wc_api")
    def test_solo_la_propina_llega_si_los_productos_estan_en_cero(self, mock_wc):
        mock_wc.get_product.return_value = {"sku": "500"}
        items = [self._make_item(1, 1, "0", "0")]
        fee_lines = [{"name": "Tip", "total": "5000"}]
        products, _ = build_sale_products(1, items, fee_lines, discount=0)
        self.assertEqual(products, [{"product_id": 100, "quantity": 1.0, "price": 5000.0}])

    @patch("core.services.wc_api")
    def test_item_con_precio_negativo_es_omitido(self, mock_wc):
        mock_wc.get_product.return_value = {"sku": "500"}
        items = [self._make_item(1, 1, "-5000", "0")]
        products, skipped = build_sale_products(1, items, [], discount=0)
        self.assertEqual(products, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("negativo", skipped[0])

    @patch("core.services.wc_api")
    def test_omitido_por_negativo_no_cuenta_como_precio_cero(self, mock_wc):
        # El mensaje del negativo no debe confundirse con el del precio 0: de eso
        # depende que la orden se registre como fallo y no como descarte limpio.
        from core.services import _all_skips_are_zero_price

        mock_wc.get_product.return_value = {"sku": "500"}
        items = [self._make_item(1, 1, "-5000", "0")]
        _, skipped = build_sale_products(1, items, [], discount=0)
        self.assertFalse(_all_skips_are_zero_price(skipped))

    @patch("core.services.wc_api")
    def test_propina_negativa_es_omitida(self, mock_wc):
        mock_wc.get_product.return_value = {"sku": "500"}
        items = [self._make_item(1, 1, "10000", "1000")]
        fee_lines = [{"name": "Tip", "total": "-5000"}]
        products, skipped = build_sale_products(1, items, fee_lines, discount=0)
        self.assertEqual([p for p in products if p["product_id"] == 100], [])
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


class ProcessOrderZeroTotalTest(TestCase):
    """Ninguna orden con total 0 debe llegar a BIMS (con o sin descuento)."""

    def _order(self, total, discount_total):
        return {
            "total": total,
            "discount_total": discount_total,
            "meta_data": [],
            "billing": {},
            "shipping": {},
            "line_items": [
                {"product_id": 182134, "quantity": 1, "total": "0", "total_tax": "0"}
            ],
            "fee_lines": [],
        }

    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_orden_total_cero_sin_descuento_no_llega_a_bims(self, mock_wc, mock_bims):
        from core.services import process_order

        mock_wc.get_order.return_value = self._order(total="0", discount_total="0")
        # SKU válido: sin la guarda correcta, se armaría un producto y se llamaría a create_sale.
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (12345, 777, None)

        process_order(order_id=183527)

        mock_bims.create_sale.assert_not_called()

    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_total_decimal_cero_se_trata_como_monto_cero(self, mock_wc, mock_bims):
        from core.services import process_order

        # WooCommerce puede enviar el total como string decimal ("0.00").
        # int("0.00") lanza ValueError; debe tratarse como monto 0, no romper.
        mock_wc.get_order.return_value = self._order(total="0.00", discount_total="0.00")
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (12345, 777, None)

        result = process_order(order_id=999)

        mock_bims.create_sale.assert_not_called()
        self.assertEqual(result["status"], "Monto 0")

    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_cortesia_con_total_cero_sigue_sin_llegar_a_bims(self, mock_wc, mock_bims):
        """
        Alcance acordado con finanzas (2026-08-27): solo las cortesías que ya traen
        precio. Las 812 históricas con las líneas en 0 quedan afuera, y las frena el
        chequeo de `total == 0`, que corre ANTES del de método de pago. Este test
        existe para que sacar el descarte de Cortesía no las cuele por la ventana.
        """
        from core.services import process_order

        orden = self._order(total="0", discount_total="0")
        orden["payment_method_title"] = "Cortesía"
        mock_wc.get_order.return_value = orden
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (12345, 777, None)

        result = process_order(order_id=201334)

        mock_bims.create_sale.assert_not_called()
        self.assertEqual(result["status"], "Monto 0")


class ProcessOrderZeroPriceItemsTest(TestCase):
    """Los productos con precio 0 no llegan a BIMS; si no queda ninguno, la orden se descarta."""

    def _order(self, line_items, fee_lines=None, total="10000"):
        return {
            "total": total,
            "discount_total": "0",
            "meta_data": [],
            "billing": {},
            "shipping": {},
            "line_items": line_items,
            "fee_lines": fee_lines or [],
        }

    def _item(self, product_id, total, total_tax="0", quantity=1, name="Producto"):
        return {
            "product_id": product_id,
            "variation_id": 0,
            "quantity": quantity,
            "total": total,
            "total_tax": total_tax,
            "name": name,
        }

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_todos_los_productos_en_cero_no_llega_a_bims(self, mock_wc, mock_bims, _mock_contact):
        from core.services import process_order

        mock_wc.get_order.return_value = self._order([self._item(1, "0"), self._item(2, "0")])
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (12345, 777, None)

        result = process_order(order_id=555)

        mock_bims.create_sale.assert_not_called()
        self.assertEqual(result["status"], "Productos en 0")
        # Antes esta orden NO dejaba fila, y esa ausencia era ambigua: "no está
        # la meta" podía significar "no correspondía facturar" o "se perdió".
        # Ahora deja una fila terminal en NOT_APPLICABLE. Ver spec §1.3.
        self.assertEqual(FailedOrder.objects.count(), 1)
        self.assertEqual(
            FailedOrder.objects.get(order_id=555).status, FailedOrder.NOT_APPLICABLE
        )

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_producto_en_cero_junto_a_uno_sin_sku_se_registra_como_fallo(
        self, mock_wc, mock_bims, _mock_contact
    ):
        from core.services import process_order

        mock_wc.get_order.return_value = self._order([self._item(1, "0"), self._item(2, "5000")])
        # El segundo ítem tiene monto pero no tiene SKU: es un problema real que revisar.
        mock_wc.get_product.side_effect = [{"sku": "500"}, {"sku": ""}]
        mock_bims.create_sale.return_value = (12345, 777, None)

        with self.assertRaises(ValueError):
            process_order(order_id=556)

        mock_bims.create_sale.assert_not_called()
        self.assertEqual(FailedOrder.objects.filter(order_id=556).count(), 1)

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_solo_la_propina_llega_si_los_productos_estan_en_cero(
        self, mock_wc, mock_bims, _mock_contact
    ):
        from core.services import process_order

        mock_wc.get_order.return_value = self._order(
            [self._item(1, "0")], fee_lines=[{"name": "Tip", "total": "5000"}]
        )
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (12345, 777, None)

        process_order(order_id=557)

        _, kwargs = mock_bims.create_sale.call_args
        self.assertEqual(
            kwargs["sale_products"], [{"product_id": 100, "quantity": 1.0, "price": 5000.0}]
        )

    @patch("core.services.sentry_sdk")
    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_item_en_cero_no_genera_warning_en_sentry(
        self, mock_wc, mock_bims, _mock_contact, mock_sentry
    ):
        from core.services import process_order

        # Un producto gratis junto a uno pago es normal: la venta llega bien a BIMS
        # y no hay nada que revisar, así que no debe ensuciar Sentry.
        mock_wc.get_order.return_value = self._order([self._item(1, "0"), self._item(2, "5000")])
        mock_wc.get_product.side_effect = [{"sku": "500"}, {"sku": "600"}]
        mock_bims.create_sale.return_value = (12345, 777, None)

        process_order(order_id=558)

        mock_bims.create_sale.assert_called_once()
        mock_sentry.capture_message.assert_not_called()

    @patch("core.services.sentry_sdk")
    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_item_negativo_se_omite_y_alerta_en_sentry(
        self, mock_wc, mock_bims, _mock_contact, mock_sentry
    ):
        from core.services import process_order

        # Un precio negativo suele ser una linea de descuento mal armada: la venta
        # sale con el resto, pero alguien tiene que revisarla.
        mock_wc.get_order.return_value = self._order([self._item(1, "-5000"), self._item(2, "8000")])
        mock_wc.get_product.side_effect = [{"sku": "500"}, {"sku": "600"}]
        mock_bims.create_sale.return_value = (12345, 777, None)

        process_order(order_id=560)

        _, kwargs = mock_bims.create_sale.call_args
        self.assertEqual(
            kwargs["sale_products"], [{"product_id": 600, "quantity": 1, "price": 8000.0}]
        )
        mock_sentry.capture_message.assert_called_once()

    @patch("core.services.sentry_sdk")
    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_orden_con_solo_items_negativos_se_registra_como_fallo(
        self, mock_wc, mock_bims, _mock_contact, mock_sentry
    ):
        from core.services import process_order

        # Sin productos validos y con un negativo de por medio no es un descarte
        # esperado: queda en FailedOrder para revisar.
        mock_wc.get_order.return_value = self._order([self._item(1, "-5000")])
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (12345, 777, None)

        with self.assertRaises(ValueError):
            process_order(order_id=561)

        mock_bims.create_sale.assert_not_called()
        self.assertEqual(FailedOrder.objects.filter(order_id=561).count(), 1)

    @patch("core.services.sentry_sdk")
    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_item_sin_sku_sigue_generando_warning_en_sentry(
        self, mock_wc, mock_bims, _mock_contact, mock_sentry
    ):
        from core.services import process_order

        # Un ítem con monto pero sin SKU sí es un problema de datos que hay que revisar.
        mock_wc.get_order.return_value = self._order([self._item(1, "3000"), self._item(2, "5000")])
        mock_wc.get_product.side_effect = [{"sku": ""}, {"sku": "600"}]
        mock_bims.create_sale.return_value = (12345, 777, None)

        process_order(order_id=559)

        mock_bims.create_sale.assert_called_once()
        mock_sentry.capture_message.assert_called_once()


class SucursalResolucionTest(TestCase):
    """
    `resolve_pos_and_payments` resuelve el punto de venta leyendo la BD.

    Antes vivía hardcodeado en `core/constants.py`, así que agregar una sucursal
    exigía editar código y redesplegar. Las constantes quedan como red de
    seguridad: si la tabla está vacía o la consulta falla se usan ellas, porque
    una tabla nueva no puede tener el poder de frenar la facturación.
    """

    _PAGO_EFECTIVO = '[{"opmk": "fooeventspos_cash", "amount": 0}]'

    def _pos_meta(self, user_id: int) -> list:
        return [
            {"key": "_fooeventspos_user_id", "value": str(user_id)},
            {"key": "_fooeventspos_payments", "value": self._PAGO_EFECTIVO},
        ]

    # ── La siembra reproduce el estado previo al cambio ──────────────────────

    def test_la_migracion_siembra_las_sucursales_actuales(self):
        self.assertEqual(Sucursal.objects.get(wp_user_id=729).bims_posale_id, 4)
        self.assertEqual(Sucursal.objects.get(wp_user_id=3).bims_posale_id, 1)
        self.assertIsNone(Sucursal.objects.get(wp_user_id=2).bims_posale_id)
        self.assertEqual(
            Sucursal.objects.get(tipo=Sucursal.POS_SIN_MAPEO).bims_posale_id,
            POS_DEFAULT_POSALE_ID,
        )
        self.assertEqual(
            Sucursal.objects.get(tipo=Sucursal.WEB).bims_posale_id, WEB_POSALE_ID
        )

    # ── Resolución desde la BD ──────────────────────────────────────────────

    def test_cajero_registrado_resuelve_su_punto_de_venta(self):
        posale_id, _ = resolve_pos_and_payments(
            self._pos_meta(729), total=30000, payment_method_title="Cash"
        )
        self.assertEqual(posale_id, 4)

    def test_sucursal_nueva_cargada_en_la_bd_se_usa_sin_redesplegar(self):
        """El objetivo del cambio: alta por pantalla, sin tocar código."""
        Sucursal.objects.create(
            tipo=Sucursal.CAJERO,
            nombre="Sucursal Nueva",
            email="nueva@muci.org",
            wp_user_id=1500,
            bims_posale_id=9,
        )
        posale_id, _ = resolve_pos_and_payments(
            self._pos_meta(1500), total=30000, payment_method_title="Cash"
        )
        self.assertEqual(posale_id, 9)

    def test_cajero_sin_punto_de_venta_no_se_factura(self):
        """Reemplaza el `if user_id_value == 2` hardcodeado: ahora es dato."""
        self.assertIsNone(
            resolve_pos_and_payments(
                self._pos_meta(2), total=30000, payment_method_title="Cash"
            )
        )

    def test_cualquier_cajero_puede_marcarse_como_no_facturable(self):
        Sucursal.objects.filter(wp_user_id=729).update(bims_posale_id=None)
        self.assertIsNone(
            resolve_pos_and_payments(
                self._pos_meta(729), total=30000, payment_method_title="Cash"
            )
        )

    def test_cajero_no_registrado_usa_la_fila_pos_sin_mapeo(self):
        Sucursal.objects.filter(tipo=Sucursal.POS_SIN_MAPEO).update(bims_posale_id=99)
        posale_id, _ = resolve_pos_and_payments(
            self._pos_meta(4321), total=15000, payment_method_title="Cash"
        )
        self.assertEqual(posale_id, 99)

    def test_orden_web_usa_la_fila_web(self):
        Sucursal.objects.filter(tipo=Sucursal.WEB).update(bims_posale_id=88)
        posale_id, _ = resolve_pos_and_payments(
            [], total=30000, payment_method_title="Credit Card"
        )
        self.assertEqual(posale_id, 88)

    # ── Red de seguridad ────────────────────────────────────────────────────

    def test_tabla_vacia_cae_a_las_constantes(self):
        Sucursal.objects.all().delete()
        posale_pos, _ = resolve_pos_and_payments(
            self._pos_meta(729), total=30000, payment_method_title="Cash"
        )
        self.assertEqual(posale_pos, POS_USER_ID_TO_POSALE[729])
        posale_web, _ = resolve_pos_and_payments(
            [], total=30000, payment_method_title="Credit Card"
        )
        self.assertEqual(posale_web, WEB_POSALE_ID)

    def test_tabla_vacia_el_administrador_sigue_sin_facturarse(self):
        """`POS_USER_ID_TO_POSALE[2]` es None: un `.get()` ingenuo lo trataría
        como 'no encontrado' y le asignaría el punto de venta por defecto."""
        Sucursal.objects.all().delete()
        self.assertIsNone(
            resolve_pos_and_payments(
                self._pos_meta(2), total=30000, payment_method_title="Cash"
            )
        )

    def test_cortesia_ya_no_se_ignora(self):
        """El descarte por Cortesía se quitó a pedido de finanzas (2026-08-27)."""
        self.assertIsNotNone(
            resolve_pos_and_payments(
                self._pos_meta(729), total=30000, payment_method_title="Cortesía"
            )
        )


class SucursalDatosWooTest(TestCase):
    """
    Completar email ↔ id de cajero contra WooCommerce.

    Verificado contra la API viva el 2026-08-24: `customers/729` devuelve
    `sancosmos@muci.org` con rol `fooeventspos_cashier`, y la búsqueda por email
    devuelve exactamente un resultado. Las dos direcciones funcionan, así que
    alcanza con cargar una de las dos.
    """

    def _sucursal(self, **kwargs):
        base = dict(tipo=Sucursal.CAJERO, nombre="Prueba")
        base.update(kwargs)
        return Sucursal(**base)

    @patch("core.sucursales.wc_api")
    def test_completa_el_id_del_cajero_a_partir_del_email(self, mock_wc):
        mock_wc.find_customer_by_email.return_value = {
            "id": 1500,
            "email": "nueva@muci.org",
            "role": "fooeventspos_cashier",
        }
        sucursal = self._sucursal(email="nueva@muci.org")
        aviso = completar_desde_woocommerce(sucursal)
        self.assertIsNone(aviso)
        self.assertEqual(sucursal.wp_user_id, 1500)

    @patch("core.sucursales.wc_api")
    def test_completa_el_email_a_partir_del_id(self, mock_wc):
        mock_wc.get_customer.return_value = {
            "id": 729,
            "email": "sancosmos@muci.org",
            "role": "fooeventspos_cashier",
        }
        sucursal = self._sucursal(wp_user_id=729)
        aviso = completar_desde_woocommerce(sucursal)
        self.assertIsNone(aviso)
        self.assertEqual(sucursal.email, "sancosmos@muci.org")

    @patch("core.sucursales.wc_api")
    def test_avisa_si_el_email_no_existe_en_woocommerce(self, mock_wc):
        mock_wc.find_customer_by_email.return_value = None
        sucursal = self._sucursal(email="fantasma@muci.org")
        aviso = completar_desde_woocommerce(sucursal)
        self.assertIn("fantasma@muci.org", aviso)
        self.assertIsNone(sucursal.wp_user_id)

    @patch("core.sucursales.wc_api")
    def test_avisa_si_el_usuario_no_es_cajero_pero_igual_lo_carga(self, mock_wc):
        """No lo rechazamos: el rol puede cambiar y no queremos bloquear el alta."""
        mock_wc.find_customer_by_email.return_value = {
            "id": 55,
            "email": "alguien@muci.org",
            "role": "customer",
        }
        sucursal = self._sucursal(email="alguien@muci.org")
        aviso = completar_desde_woocommerce(sucursal)
        self.assertIn("customer", aviso)
        self.assertEqual(sucursal.wp_user_id, 55)

    @patch("core.sucursales.wc_api")
    def test_si_woocommerce_falla_avisa_y_no_lanza(self, mock_wc):
        """El alta no puede depender de que WooCommerce esté arriba."""
        mock_wc.find_customer_by_email.side_effect = requests.ConnectionError("down")
        sucursal = self._sucursal(email="nueva@muci.org")
        aviso = completar_desde_woocommerce(sucursal)
        self.assertIn("WooCommerce", aviso)
        self.assertIsNone(sucursal.wp_user_id)

    @patch("core.sucursales.wc_api")
    def test_no_consulta_woocommerce_para_filas_sin_cajero(self, mock_wc):
        """Las filas `web` y `pos_sin_mapeo` no tienen usuario que resolver."""
        for tipo in (Sucursal.WEB, Sucursal.POS_SIN_MAPEO):
            sucursal = Sucursal(tipo=tipo, nombre="Regla", bims_posale_id=6)
            self.assertIsNone(completar_desde_woocommerce(sucursal))
        mock_wc.get_customer.assert_not_called()
        mock_wc.find_customer_by_email.assert_not_called()


_TEST_API_KEY = "clave_de_prueba_123"


class ApiKeyAuthTest(TestCase):
    """
    Autenticación por API Key en header vs. el modo legacy por sesión (?sid=).

    BIMS corta el login por usuario+password el 30/09/2026. La key va cruda,
    sin prefijo de tenant (verificado contra la API viva el 2026-08-21).

    Los tests interceptan `requests.Session.send`, así que asertan sobre la
    request realmente preparada —URL y headers— y no sobre mocks.
    """

    def _fake_send(self, captured, body):
        """Intercepta el transporte, guarda la request real y devuelve `body`."""

        def fake_send(session_self, request, **kwargs):
            captured.append(request)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(body).encode("utf-8")
            response.request = request
            return response

        return fake_send

    def _build_api(self):
        """Instancia BimsApi sin dejar que un login real salga a la red."""
        with patch.object(BimsApi, "login", return_value="sid_que_no_deberia_usarse"):
            return BimsApi()

    _CONTACT_OK = {"status": "ok", "count": 1, "data": [{"Contact": {"id": "7"}}]}

    # ── Modo API Key ────────────────────────────────────────────────────────

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_con_api_key_no_hace_login(self):
        with patch.object(BimsApi, "login") as mock_login:
            api = BimsApi()
        mock_login.assert_not_called()
        self.assertIsNone(api.sid)

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_con_api_key_el_header_viaja_a_bims(self):
        api = self._build_api()
        captured = []
        with patch.object(requests.Session, "send", self._fake_send(captured, self._CONTACT_OK)):
            api.list_contacts("123456", "CI")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].headers.get("X-API-Key"), _TEST_API_KEY)

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_con_api_key_la_url_no_lleva_sid(self):
        api = self._build_api()
        captured = []
        with patch.object(requests.Session, "send", self._fake_send(captured, self._CONTACT_OK)):
            api.list_contacts("123456", "CI")
        self.assertNotIn("sid=", captured[0].url)

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    @patch("core.bims.time.sleep")
    def test_con_api_key_un_401_es_terminal_y_no_relogea(self, mock_sleep):
        """Sin sesión que renovar, un 401 es rechazo de negocio: ni relogin ni reintentos."""
        api = self._build_api()
        captured = []
        body = {"status": "error", "code": "401", "message": "Unauthorized"}
        with patch.object(BimsApi, "login") as mock_login:
            with patch.object(requests.Session, "send", self._fake_send(captured, body)):
                with self.assertRaises(BimsBusinessError):
                    api.list_contacts("123456", "CI")
        mock_login.assert_not_called()
        self.assertEqual(len(captured), 1)
        mock_sleep.assert_not_called()

    @override_settings(BIMS_API_KEY=_TEST_API_KEY, RUC_URL="https://turuc.test/api")
    def test_la_consulta_de_ruc_no_filtra_el_api_key_de_bims(self):
        """Guarda: turuc es un tercero, la credencial de BIMS no puede viajar ahí."""
        api = self._build_api()
        captured = []
        body = {"status": "ok", "count": 1, "data": [{"Contact": {"name": "ACME SA"}}]}
        with patch.object(requests.Session, "send", self._fake_send(captured, body)):
            api.find_razon_social_by_ruc("80012345")
        self.assertEqual(len(captured), 1)
        self.assertIn("turuc.test", captured[0].url)
        self.assertNotIn("X-API-Key", captured[0].headers)

    # ── Modo sesión (legacy), debe seguir intacto ───────────────────────────

    @override_settings(BIMS_API_KEY="")
    def test_sin_api_key_sigue_usando_sid_y_sin_header(self):
        with patch.object(BimsApi, "login", return_value="sid_legacy") as mock_login:
            api = BimsApi()
        mock_login.assert_called_once()
        self.assertEqual(api.sid, "sid_legacy")
        captured = []
        with patch.object(requests.Session, "send", self._fake_send(captured, self._CONTACT_OK)):
            api.list_contacts("123456", "CI")
        self.assertIn("sid=sid_legacy", captured[0].url)
        self.assertNotIn("X-API-Key", captured[0].headers)


def _raw_with_set_cookie(set_cookie: str):
    """
    `response.raw` mínimo para que `requests` extraiga cookies de verdad:
    `extract_cookies_to_jar` solo mira `raw._original_response.msg`.
    """
    return SimpleNamespace(
        _original_response=SimpleNamespace(
            msg=email.message_from_string("Set-Cookie: {}\r\n".format(set_cookie))
        )
    )


class SessionCookiesTest(TestCase):
    """
    La `requests.Session` del cliente no debe acarrear cookies de BIMS.

    `81eb9ba` cambió los 10 métodos de `requests.get/post` (sin estado) a
    `self.session.get/post` para ganar keep-alive, y con eso heredó un cookie
    jar que nunca fue intencional. BIMS devuelve una cookie de sesión; al
    reenviarla junto al header `X-API-Key` responde `code: 401` ("Session ID no
    coincide con la cookie de sesión activa"), que en modo API Key es terminal.
    En producción eso cortó la facturación tras el primer request de cada worker
    de gunicorn (2026-08-21, órdenes 200361 / 200365 / 200373).

    Estos tests interceptan `HTTPAdapter.send`, NO `Session.send`: la extracción
    de cookies vive dentro de `Session.send`, así que parchear ahí arriba —lo que
    hace el resto de la suite— es justo lo que impidió detectar este bug.
    """

    _BIMS_COOKIE = "CAKEPHP=6f8s0bqk9v1n; Path=/"
    _CONTACT_OK = {"status": "ok", "count": 1, "data": [{"Contact": {"id": "7"}}]}

    def _fake_adapter_send(self, captured):
        """Responde 200 con una cookie de sesión, como hace BIMS."""

        def fake_send(adapter_self, request, **kwargs):
            captured.append(request)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response._content = json.dumps(self._CONTACT_OK).encode("utf-8")
            response.raw = _raw_with_set_cookie(self._BIMS_COOKIE)
            return response

        return fake_send

    def _build_api(self):
        """Instancia BimsApi sin dejar que un login real salga a la red."""
        with patch.object(BimsApi, "login", return_value="sid_legacy"):
            return BimsApi()

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_no_guarda_la_cookie_de_sesion_que_devuelve_bims(self):
        api = self._build_api()
        captured = []
        with patch.object(requests.adapters.HTTPAdapter, "send", self._fake_adapter_send(captured)):
            api.list_contacts("123456", "CI")
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(api.session.cookies), 0)

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_el_segundo_request_no_reenvia_la_cookie(self):
        """El corte de producción: el 1er request pasaba y el 2do ya llevaba Cookie."""
        api = self._build_api()
        captured = []
        with patch.object(requests.adapters.HTTPAdapter, "send", self._fake_adapter_send(captured)):
            api.list_contacts("123456", "CI")
            api.list_contacts("123456", "CI")
        self.assertEqual(len(captured), 2)
        self.assertNotIn("Cookie", captured[1].headers)

    @override_settings(BIMS_API_KEY="")
    def test_en_modo_sesion_tampoco_acarrea_cookies(self):
        """El jar nunca fue intencional: `main` factura con `requests` pelado, sin cookies."""
        api = self._build_api()
        captured = []
        with patch.object(requests.adapters.HTTPAdapter, "send", self._fake_adapter_send(captured)):
            api.list_contacts("123456", "CI")
            api.list_contacts("123456", "CI")
        self.assertEqual(len(captured), 2)
        self.assertNotIn("Cookie", captured[1].headers)
        self.assertEqual(len(api.session.cookies), 0)


_POSALES_OK = {
    "code": "200",
    "status": "ok",
    "count": "4",
    "data": [
        {"Posale": {"id": "6", "name": "Caja WEB", "bill_code": "003", "company_id": "1"}},
        {"Posale": {"id": "4", "name": "Caja San Cosmos", "bill_code": "002", "company_id": "1"}},
        {"Posale": {"id": "1", "name": "Caja Tatakualab", "bill_code": "001", "company_id": "1"}},
        {"Posale": {"id": "7", "name": "Caja Fund MuCi", "bill_code": "004", "company_id": "1"}},
    ],
}


class GetPosalesTest(TestCase):
    """
    `BimsApi.get_posales()` lista los puntos de venta de BIMS.

    Payload verificado contra la API viva el 2026-08-24: responde `/posales/`
    (sin sufijo `.json`) con la convención CakePHP `data: [{"Posale": {...}}]`,
    y **los ids vienen como strings**.
    """

    def _fake_send(self, captured, body):
        def fake_send(session_self, request, **kwargs):
            captured.append(request)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(body).encode("utf-8")
            response.request = request
            return response

        return fake_send

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_devuelve_pares_id_nombre_con_el_id_como_entero(self):
        with patch.object(BimsApi, "login", return_value="no-deberia-usarse"):
            api = BimsApi()
        with patch.object(requests.Session, "send", self._fake_send([], _POSALES_OK)):
            posales = api.get_posales()
        self.assertEqual(
            posales,
            [
                (1, "Caja Tatakualab"),
                (4, "Caja San Cosmos"),
                (6, "Caja WEB"),
                (7, "Caja Fund MuCi"),
            ],
        )

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_pega_contra_la_ruta_posales(self):
        with patch.object(BimsApi, "login", return_value="x"):
            api = BimsApi()
        captured = []
        with patch.object(requests.Session, "send", self._fake_send(captured, _POSALES_OK)):
            api.get_posales()
        self.assertIn("/posales/", captured[0].url)

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_ignora_filas_sin_id(self):
        cuerpo = {"status": "ok", "data": [{"Posale": {"name": "Sin id"}}, {"Posale": {"id": "3", "name": "Buena"}}]}
        with patch.object(BimsApi, "login", return_value="x"):
            api = BimsApi()
        with patch.object(requests.Session, "send", self._fake_send([], cuerpo)):
            self.assertEqual(api.get_posales(), [(3, "Buena")])


class OpcionesPuntoDeVentaTest(TestCase):
    """`opciones_de_punto_de_venta()` nunca lanza: el admin no puede depender de BIMS."""

    @patch("core.sucursales._bims")
    def test_con_bims_ok_devuelve_las_opciones_sin_aviso(self, mock_bims):
        mock_bims.return_value.get_posales.return_value = [(4, "Caja San Cosmos")]
        opciones, aviso = opciones_de_punto_de_venta()
        self.assertEqual(opciones, [(4, "Caja San Cosmos")])
        self.assertIsNone(aviso)

    @patch("core.sucursales._bims")
    def test_con_bims_caido_devuelve_lista_vacia_y_aviso(self, mock_bims):
        mock_bims.return_value.get_posales.side_effect = BimsTransientError("sin respuesta")
        opciones, aviso = opciones_de_punto_de_venta()
        self.assertEqual(opciones, [])
        self.assertIn("BIMS", aviso)


class SucursalFormTest(TestCase):
    """
    El punto de venta se elige de la lista de BIMS, no se escribe a mano.

    Escribirlo a mano permitía cargar un ID inexistente, y la factura fallaba
    recién en la venta. Con la lista disponible, un ID inválido es imposible.
    """

    _OPCIONES = [(1, "Caja Tatakualab"), (4, "Caja San Cosmos"), (7, "Caja Fund MuCi")]

    def _datos(self, **extra):
        base = {"tipo": Sucursal.CAJERO, "nombre": "Nueva", "email": "", "wp_user_id": 1500}
        base.update(extra)
        return base

    @patch("core.forms.opciones_de_punto_de_venta")
    def test_el_campo_es_un_desplegable_con_las_opciones_de_bims(self, mock_op):
        mock_op.return_value = (self._OPCIONES, None)
        form = SucursalForm()
        etiquetas = dict(form.fields["bims_posale_id"].choices)
        self.assertIn("4 — Caja San Cosmos", etiquetas.values())
        self.assertEqual(len(etiquetas), len(self._OPCIONES) + 1)  # + la opcion vacia

    @patch("core.forms.opciones_de_punto_de_venta")
    def test_un_id_fuera_de_la_lista_se_rechaza(self, mock_op):
        mock_op.return_value = (self._OPCIONES, None)
        form = SucursalForm(data=self._datos(bims_posale_id=99))
        self.assertFalse(form.is_valid())
        self.assertIn("bims_posale_id", form.errors)

    @patch("core.forms.opciones_de_punto_de_venta")
    def test_un_id_de_la_lista_se_acepta(self, mock_op):
        mock_op.return_value = (self._OPCIONES, None)
        form = SucursalForm(data=self._datos(bims_posale_id=4))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["bims_posale_id"], 4)

    @patch("core.forms.opciones_de_punto_de_venta")
    def test_la_opcion_vacia_significa_no_facturar(self, mock_op):
        mock_op.return_value = (self._OPCIONES, None)
        form = SucursalForm(data=self._datos(bims_posale_id=""))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["bims_posale_id"])

    @patch("core.forms.opciones_de_punto_de_venta")
    def test_con_bims_caido_degrada_a_numerico_y_deja_guardar(self, mock_op):
        """Si BIMS no responde, el alta no se bloquea: se carga a mano y se avisa."""
        mock_op.return_value = ([], "No se pudo traer la lista de BIMS (timeout).")
        form = SucursalForm(data=self._datos(bims_posale_id=99))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["bims_posale_id"], 99)
        self.assertIn("BIMS", form.aviso_bims)


class TimeoutsBimsTest(TestCase):
    """
    Toda llamada a BIMS lleva timeout, y los reintentos tienen presupuesto.

    Antes solo `login()` tenía timeout: las otras 12 llamadas usaban
    `self.session.get/post` sin él, así que un BIMS que acepta la conexión y no
    responde bloqueaba un worker **sin límite**. Con `--workers 3`, tres de esas
    y el integrador dejaba de atender — el motivo por el que existe el reinicio
    cada 6 horas en el cron.

    Y un timeout por request no alcanza: 5 reintentos más la conmutación de host
    pueden pasar los `--timeout 120` de gunicorn, que mata al worker **por
    señal**. Un worker matado por señal no ejecuta el `except` que graba el
    `FailedOrder`, así que la orden desaparece sin factura y sin registro. De ahí
    el presupuesto global.

    Los tests interceptan `HTTPAdapter.send`, que es donde `timeout` llega de
    verdad: no viaja dentro de la PreparedRequest.
    """

    _CONTACTO_OK = {"status": "ok", "count": 1, "data": [{"Contact": {"id": "7"}}]}

    def _api(self):
        with patch.object(BimsApi, "login", return_value="sid"):
            return BimsApi()

    def _send_ok(self, capturados):
        def fake_send(adapter_self, request, **kwargs):
            capturados.append(kwargs.get("timeout"))
            respuesta = requests.Response()
            respuesta.status_code = 200
            respuesta._content = json.dumps(self._CONTACTO_OK).encode("utf-8")
            respuesta.request = request
            return respuesta

        return fake_send

    def _escenario_sin_respuesta(self, capturados, paso=15.0):
        """
        Cada intento consume `paso` segundos de reloj y falla por red.

        El reloj lo mueven el propio envío y el `sleep` parcheado, así que el
        presupuesto se agota igual que en producción pero sin esperar de verdad.
        """
        reloj = {"t": 0.0}

        def monotonic():
            return reloj["t"]

        def dormir(segundos):
            reloj["t"] += segundos

        def fake_send(adapter_self, request, **kwargs):
            capturados.append(kwargs.get("timeout"))
            reloj["t"] += paso
            raise requests.ConnectionError("BIMS no responde")

        return monotonic, dormir, fake_send

    # ── Timeout por request ─────────────────────────────────────────────────

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_toda_llamada_a_bims_lleva_timeout(self):
        api = self._api()
        capturados = []
        with patch.object(requests.adapters.HTTPAdapter, "send", self._send_ok(capturados)):
            api.list_contacts("123456", "CI")
        self.assertEqual(capturados, [(TIMEOUT_CONEXION, TIMEOUT_LECTURA)])

    @override_settings(BIMS_API_KEY="")
    def test_el_login_lleva_timeout_de_conexion_y_de_lectura(self):
        """Antes tenía `timeout=30`: un número solo, sin límite de conexión."""
        capturados = []
        cuerpo = {"status": "ok", "data": {"Session": {"id": "sid"}}}

        def fake_send(adapter_self, request, **kwargs):
            capturados.append(kwargs.get("timeout"))
            respuesta = requests.Response()
            respuesta.status_code = 200
            respuesta._content = json.dumps(cuerpo).encode("utf-8")
            respuesta.request = request
            return respuesta

        with patch.object(requests.adapters.HTTPAdapter, "send", fake_send):
            BimsApi()
        self.assertEqual(capturados, [(TIMEOUT_CONEXION, TIMEOUT_LECTURA)])

    # ── Presupuesto global ──────────────────────────────────────────────────

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_el_presupuesto_corta_los_reintentos_y_falla_limpio(self):
        """
        Falla con `BimsTransientError` en vez de agotar los 5 intentos: eso es lo
        que permite que `process_order` grabe el `FailedOrder`.
        """
        api = self._api()
        capturados = []
        monotonic, dormir, fake_send = self._escenario_sin_respuesta(capturados)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.bims.time.sleep", dormir
        ), patch.object(requests.adapters.HTTPAdapter, "send", fake_send):
            with self.assertRaises(BimsTransientError):
                api.list_contacts("123456", "CI")
        # 40 s de presupuesto y 17 s por vuelta (15 de request + 2 de espera):
        # entran 3 intentos, no los 5 de max_retries.
        self.assertEqual(len(capturados), 3)

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_el_timeout_de_lectura_se_recorta_al_presupuesto_restante(self):
        """
        Sin esto el presupuesto sería decorativo: un intento que arranca a los
        39 s podría correr hasta los 69.
        """
        api = self._api()
        capturados = []
        monotonic, dormir, fake_send = self._escenario_sin_respuesta(capturados)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.bims.time.sleep", dormir
        ), patch.object(requests.adapters.HTTPAdapter, "send", fake_send):
            with self.assertRaises(BimsTransientError):
                api.list_contacts("123456", "CI")
        self.assertEqual(capturados, [(5, 30), (5, 23), (5, 6)])

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_la_conmutacion_de_host_comparte_el_presupuesto(self):
        """No recibe un presupuesto nuevo: si se agotó, no se prueba el otro host."""
        api = self._api()
        api.fallback_url = "http://otro.bims.test.local"
        capturados = []
        monotonic, dormir, fake_send = self._escenario_sin_respuesta(capturados)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.bims.time.sleep", dormir
        ), patch.object(requests.adapters.HTTPAdapter, "send", fake_send):
            with self.assertRaises(BimsTransientError):
                api.list_contacts("123456", "CI")
        self.assertEqual(len(capturados), 3)

    def test_el_presupuesto_entra_en_la_ventana_de_gunicorn(self):
        """Con 2-3 llamadas a BIMS por orden, tiene que quedar margen bajo 120 s."""
        self.assertLessEqual(PRESUPUESTO_REINTENTOS * 3, 120)
        self.assertGreater(TIMEOUT_LECTURA, 12.47)  # el request legítimo más lento medido


class PresupuestoOrdenBimsTest(TestCase):
    """
    El presupuesto de la orden se impone por encima del de cada llamada.

    Hoy `PRESUPUESTO_REINTENTOS` es por LLAMADA: una orden hace 2-3 llamadas y
    3 x 41 s = 123 s > los 120 s de gunicorn. Al pasarse, gunicorn mata al worker
    por señal y el `FailedOrder` no se graba.
    """

    _CONTACTO_OK = {"status": "ok", "count": 1, "data": [{"Contact": {"id": "7"}}]}

    def _api(self):
        with patch.object(BimsApi, "login", return_value="sid"):
            return BimsApi()

    def _reloj_y_send(self, capturados, paso=15.0, responder_ok=False):
        """
        Reloj falso compartido por core.bims y core.deadline.

        Devuelve (monotonic, dormir, fake_send). Cada envío consume `paso`
        segundos; si `responder_ok`, responde 200 en vez de fallar por red.
        """
        reloj = {"t": 1000.0}

        def monotonic():
            return reloj["t"]

        def dormir(segundos):
            reloj["t"] += segundos

        def fake_send(adapter_self, request, **kwargs):
            capturados.append(kwargs.get("timeout"))
            reloj["t"] += paso
            if responder_ok:
                respuesta = requests.Response()
                respuesta.status_code = 200
                respuesta._content = json.dumps(self._CONTACTO_OK).encode("utf-8")
                respuesta.request = request
                return respuesta
            raise requests.ConnectionError("BIMS no responde")

        return monotonic, dormir, fake_send

    # ── Sin presupuesto: nada cambia ────────────────────────────────────────

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_sin_presupuesto_de_orden_el_timeout_es_el_de_siempre(self):
        """
        Protege a `sync_bims_contacts`: 38 llamadas secuenciales sin deadline.
        """
        api = self._api()
        capturados = []
        with patch.object(requests.adapters.HTTPAdapter, "send", self._send_ok_simple(capturados)):
            api.list_contacts("123456", "CI")
        self.assertEqual(capturados, [(TIMEOUT_CONEXION, TIMEOUT_LECTURA)])

    def _send_ok_simple(self, capturados):
        def fake_send(adapter_self, request, **kwargs):
            capturados.append(kwargs.get("timeout"))
            respuesta = requests.Response()
            respuesta.status_code = 200
            respuesta._content = json.dumps(self._CONTACTO_OK).encode("utf-8")
            respuesta.request = request
            return respuesta

        return fake_send

    # ── Con presupuesto: se recorta ──────────────────────────────────────────

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_el_timeout_se_recorta_al_restante_de_la_orden(self):
        """
        Con 8 s de orden restantes, la lectura no puede ser de 30 s aunque el
        presupuesto de la llamada tenga 40 s enteros.
        """
        api = self._api()
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados, responder_ok=True)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            token = deadline.iniciar(8)
            try:
                api.list_contacts("123456", "CI")
            finally:
                deadline.restaurar(token)
        self.assertEqual(capturados, [(5, 8)])

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_el_timeout_de_conexion_tambien_se_recorta(self):
        """
        Hallazgo 2 de la revisión: hoy solo se recorta la lectura, así que un
        intento que arranca al límite puede excederse hasta 5 s por la conexión.
        Con el presupuesto de orden en juego ese exceso deja de ser cosmético.
        """
        api = self._api()
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados, responder_ok=True)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            token = deadline.iniciar(3)
            try:
                api.list_contacts("123456", "CI")
            finally:
                deadline.restaurar(token)
        self.assertEqual(capturados, [(3, 3)])

    # ── Agotado: corta seco ──────────────────────────────────────────────────

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_presupuesto_agotado_lanza_la_excepcion_propia(self):
        api = self._api()
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            token = deadline.iniciar(10)
            try:
                with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                    api.list_contacts("123456", "CI")
            finally:
                deadline.restaurar(token)
        # Un solo intento: consume los 15 s del envío más 2 de espera, así que el
        # segundo arranca con el presupuesto de orden (10 s) ya en negativo. El de
        # la llamada todavía tendría 23 s — la orden es la que corta.
        self.assertEqual(len(capturados), 1)

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_presupuesto_agotado_no_conmuta_de_host(self):
        """No queda tiempo para probar el otro host."""
        api = self._api()
        api.fallback_url = "http://otro.bims.test.local"
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            token = deadline.iniciar(10)
            try:
                with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                    api.list_contacts("123456", "CI")
            finally:
                deadline.restaurar(token)
        # Con fallback configurada seguiría habiendo un solo envío: agotado el
        # presupuesto de la orden no se prueba el otro host.
        self.assertEqual(len(capturados), 1)

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_el_error_conserva_la_causa_real(self):
        """
        Hallazgo 4: al agotarse el presupuesto de la LLAMADA con fallback
        configurada, se reentraba al loop con el mismo límite y el segundo loop
        fallaba de inmediato con `Last error: None`, perdiendo la causa.
        """
        api = self._api()
        api.fallback_url = "http://otro.bims.test.local"
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            with self.assertRaises(BimsTransientError) as ctx:
                api.list_contacts("123456", "CI")
        self.assertNotIn("Last error: None", str(ctx.exception))
        self.assertIn("BIMS no responde", str(ctx.exception))

    @override_settings(BIMS_API_KEY=_TEST_API_KEY)
    def test_agotado_el_presupuesto_no_deja_el_base_url_conmutado(self):
        """
        Hallazgo 1: `_alternate_base_url` no es una consulta pura, muta
        `self.base_url` de forma pegajosa (sticky para el resto de la vida de
        la instancia). Si el presupuesto de la LLAMADA ya se agotó, no se manda
        ningún request al host alternativo, así que tampoco debe quedar la
        instancia apuntando ahí para las próximas órdenes de este worker.
        """
        api = self._api()
        api.fallback_url = "http://otro.bims.test.local"
        base_url_original = api.base_url
        capturados = []
        monotonic, dormir, fake_send = self._reloj_y_send(capturados)
        with patch("core.bims.time.monotonic", monotonic), patch(
            "core.deadline.time.monotonic", monotonic
        ), patch("core.bims.time.sleep", dormir), patch.object(
            requests.adapters.HTTPAdapter, "send", fake_send
        ):
            with self.assertRaises(BimsTransientError):
                api.list_contacts("123456", "CI")
        self.assertEqual(api.base_url, base_url_original)

    # ── La garantía agregada ──────────────────────────────────────────────────

    def test_tres_llamadas_no_superan_el_presupuesto_de_la_orden(self):
        """
        El cálculo que motiva la spec: 3 x 41 s = 123 s > 120 de gunicorn. Con el
        presupuesto de orden, el techo agregado es PRESUPUESTO_ORDEN, no 3 x 41.
        """
        self.assertLess(deadline.PRESUPUESTO_ORDEN, 120)
        self.assertGreater(PRESUPUESTO_REINTENTOS * 3, deadline.PRESUPUESTO_ORDEN)


class TimeoutWooCommerceTest(TestCase):
    """
    El timeout de WooCommerce tiene que ser menor al de gunicorn.

    Estaba en 480 s — cuatro veces los 120 s de gunicorn, así que cualquier
    llamada lenta hacía que gunicorn matara al worker en el medio, perdiendo la
    orden sin grabar el `FailedOrder`.
    """

    def test_es_menor_al_timeout_de_gunicorn(self):
        self.assertLess(TIMEOUT_WOOCOMMERCE, 120)

    def test_el_cliente_se_construye_con_ese_timeout(self):
        self.assertEqual(WooCommerceAPI().wcapi.timeout, TIMEOUT_WOOCOMMERCE)


class PresupuestoOrdenWooCommerceTest(TestCase):
    """
    WooCommerce también consume presupuesto de la orden.

    `get_product` se llama una vez por ítem, así que este tramo escala con la
    cantidad de ítems y hoy no tiene ningún techo agregado: una orden de 5 ítems
    puede gastar 5 x 30 = 150 s antes de tocar BIMS.
    """

    def _reloj(self):
        estado = {"t": 1000.0}

        def monotonic():
            return estado["t"]

        def avanzar(segundos):
            estado["t"] += segundos

        return monotonic, avanzar

    def test_sin_presupuesto_usa_el_timeout_completo(self):
        """Protege a cualquier uso fuera de una orden (p. ej. sync_bims_contacts)."""
        api = WooCommerceAPI()
        self.assertEqual(api._timeout_efectivo("orders/1"), TIMEOUT_WOOCOMMERCE)

    def test_con_presupuesto_amplio_usa_el_timeout_completo(self):
        """Con presupuesto en juego el resultado es tupla, aunque el tope siga siendo el de siempre."""
        api = WooCommerceAPI()
        monotonic, _ = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar(90)
            try:
                self.assertEqual(
                    api._timeout_efectivo("orders/1"),
                    (TIMEOUT_CONEXION_WOOCOMMERCE, TIMEOUT_WOOCOMMERCE),
                )
            finally:
                deadline.restaurar(token)

    def test_con_presupuesto_corto_se_recorta(self):
        api = WooCommerceAPI()
        monotonic, _ = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar(7)
            try:
                self.assertEqual(
                    api._timeout_efectivo("orders/1"),
                    (TIMEOUT_CONEXION_WOOCOMMERCE, 7),
                )
            finally:
                deadline.restaurar(token)

    def test_con_presupuesto_agotado_lanza_sin_pegarle_a_woocommerce(self):
        api = WooCommerceAPI()
        monotonic, avanzar = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar(5)
            try:
                avanzar(6)
                with self.assertRaises(deadline.PresupuestoOrdenAgotado) as ctx:
                    api._timeout_efectivo("orders/42")
            finally:
                deadline.restaurar(token)
        # Hallazgo 4: el mensaje tiene que nombrar el endpoint, igual que BIMS.
        self.assertIn("orders/42", str(ctx.exception))

    def test_get_product_aplica_el_timeout_recortado(self):
        api = WooCommerceAPI()
        monotonic, _ = self._reloj()
        respuesta = MagicMock()
        respuesta.status_code = 200
        respuesta.json.return_value = {"id": 1}
        capturado = {}

        def get_y_capturar(*args, **kwargs):
            # El timeout hay que leerlo DURANTE la llamada: hallazgo 6 lo
            # restaura a TIMEOUT_WOOCOMMERCE apenas termina.
            capturado["timeout"] = api.wcapi.timeout
            return respuesta

        with patch("core.deadline.time.monotonic", monotonic), patch.object(
            api.wcapi, "get", side_effect=get_y_capturar
        ):
            token = deadline.iniciar(9)
            try:
                api.get_product(1)
            finally:
                deadline.restaurar(token)
        self.assertEqual(capturado["timeout"], (TIMEOUT_CONEXION_WOOCOMMERCE, 9))

    def test_el_timeout_se_restaura_tras_la_llamada(self):
        """
        Hallazgo 6: sin restore, el recorte de una orden queda pegado al
        singleton `wc_api` y la siguiente orden lo hereda en silencio.
        """
        api = WooCommerceAPI()
        monotonic, _ = self._reloj()
        respuesta = MagicMock()
        respuesta.status_code = 200
        respuesta.json.return_value = {"id": 1}
        with patch("core.deadline.time.monotonic", monotonic), patch.object(
            api.wcapi, "get", return_value=respuesta
        ):
            token = deadline.iniciar(9)
            try:
                api.get_product(1)
            finally:
                deadline.restaurar(token)
        self.assertEqual(api.wcapi.timeout, TIMEOUT_WOOCOMMERCE)

    def test_la_conexion_va_acotada_por_separado_de_la_lectura(self):
        """
        Hallazgo 2: un timeout escalar en `requests` vale para conexión Y
        lectura por separado, así que sin la tupla una llamada recortada a N
        segundos podía tardar hasta 2N. Con presupuesto amplio, la conexión
        queda en su propio tope corto aunque la lectura tenga margen.
        """
        api = WooCommerceAPI()
        monotonic, _ = self._reloj()
        respuesta = MagicMock()
        respuesta.status_code = 200
        respuesta.json.return_value = {"id": 1}
        capturado = {}

        def get_y_capturar(*args, **kwargs):
            capturado["timeout"] = api.wcapi.timeout
            return respuesta

        with patch("core.deadline.time.monotonic", monotonic), patch.object(
            api.wcapi, "get", side_effect=get_y_capturar
        ):
            token = deadline.iniciar(20)
            try:
                api.get_order(1)
            finally:
                deadline.restaurar(token)
        self.assertIsInstance(capturado["timeout"], tuple)
        self.assertEqual(capturado["timeout"][0], TIMEOUT_CONEXION_WOOCOMMERCE)

    def test_una_orden_de_muchos_items_no_supera_el_presupuesto(self):
        """
        El caso que hoy escala sin techo: con el presupuesto puesto, el ítem que
        cae fuera del tiempo corta con PresupuestoOrdenAgotado en vez de seguir
        sumando 30 s por ítem hasta pasarse de gunicorn.
        """
        api = WooCommerceAPI()
        monotonic, avanzar = self._reloj()
        respuesta = MagicMock()
        respuesta.status_code = 200
        respuesta.json.return_value = {"id": 1}

        def get_lento(*args, **kwargs):
            avanzar(TIMEOUT_WOOCOMMERCE)
            return respuesta

        atendidos = 0
        with patch("core.deadline.time.monotonic", monotonic), patch.object(
            api.wcapi, "get", side_effect=get_lento
        ):
            token = deadline.iniciar(deadline.PRESUPUESTO_ORDEN)
            try:
                with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                    for _ in range(10):
                        api.get_product(1)
                        atendidos += 1
            finally:
                deadline.restaurar(token)
        # 90 s de presupuesto a 30 s por ítem: entran 3, el cuarto corta.
        self.assertEqual(atendidos, 3)


class DeadlineOrdenTest(TestCase):
    """
    El reloj por orden. `restante()` devolviendo None es la pieza central del
    diseño: "sin presupuesto" es un estado explícito y legítimo, y es lo que
    hace que `sync_bims_contacts` siga funcionando sin tocar ese archivo.
    """

    def _reloj(self):
        """Reloj falso: devuelve (monotonic, avanzar)."""
        estado = {"t": 1000.0}

        def monotonic():
            return estado["t"]

        def avanzar(segundos):
            estado["t"] += segundos

        return monotonic, avanzar

    def test_sin_iniciar_no_hay_presupuesto(self):
        self.assertIsNone(deadline.restante())

    def test_iniciar_fija_el_presupuesto_completo(self):
        monotonic, _ = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar()
            try:
                self.assertAlmostEqual(deadline.restante(), deadline.PRESUPUESTO_ORDEN)
            finally:
                deadline.restaurar(token)

    def test_el_restante_baja_con_el_reloj(self):
        monotonic, avanzar = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar()
            try:
                avanzar(30)
                self.assertAlmostEqual(deadline.restante(), deadline.PRESUPUESTO_ORDEN - 30)
            finally:
                deadline.restaurar(token)

    def test_el_restante_puede_ser_negativo(self):
        """No se recorta a 0: el consumidor distingue 'agotado' de 'sin presupuesto'."""
        monotonic, avanzar = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar()
            try:
                avanzar(deadline.PRESUPUESTO_ORDEN + 5)
                self.assertLess(deadline.restante(), 0)
            finally:
                deadline.restaurar(token)

    def test_restaurar_vuelve_a_sin_presupuesto(self):
        token = deadline.iniciar()
        deadline.restaurar(token)
        self.assertIsNone(deadline.restante())

    def test_acepta_un_presupuesto_explicito(self):
        """Los tests de las otras tareas fijan presupuestos chicos con esto."""
        monotonic, _ = self._reloj()
        with patch("core.deadline.time.monotonic", monotonic):
            token = deadline.iniciar(10)
            try:
                self.assertAlmostEqual(deadline.restante(), 10)
            finally:
                deadline.restaurar(token)

    def test_deja_margen_bajo_el_timeout_de_gunicorn(self):
        """30 s de margen para grabar el FailedOrder y responder."""
        self.assertLessEqual(deadline.PRESUPUESTO_ORDEN, 90)
        self.assertGreaterEqual(120 - deadline.PRESUPUESTO_ORDEN, 30)

    def test_la_excepcion_no_es_un_error_de_bims(self):
        """
        Si heredara de BimsTransientError, el `except` de `_retry_request` se la
        comería y volvería a intentar sin tiempo disponible.
        """
        self.assertTrue(issubclass(deadline.PresupuestoOrdenAgotado, Exception))
        self.assertFalse(issubclass(deadline.PresupuestoOrdenAgotado, BimsError))


class PresupuestoOrdenProcessOrderTest(TestCase):
    """
    `process_order` es el único que fija el presupuesto, y la garantía central es
    que agotarlo termine en un `FailedOrder` grabado — nunca en una orden que
    desaparece.
    """

    def _orden(self, items=1):
        return {
            "total": "40000.00",
            "discount_total": "0.00",
            "meta_data": [],
            "billing": {"email": "cliente@example.com", "first_name": "Ana", "last_name": "Diaz"},
            "shipping": {},
            "line_items": [
                {"product_id": 100, "quantity": 1, "total": "40000.00", "sku": "SKU1"}
                for _ in range(items)
            ],
        }

    def test_process_order_fija_el_presupuesto(self):
        """`process_order` fija el presupuesto antes de llamar a `_process_order`."""
        visto = {}

        def _capturar(order_id):
            visto["restante"] = deadline.restante()
            return {"status": "ok"}

        with patch("core.services._process_order", side_effect=_capturar):
            process_order(1)
        self.assertIsNotNone(visto["restante"])
        self.assertLessEqual(visto["restante"], deadline.PRESUPUESTO_ORDEN)
        self.assertGreater(visto["restante"], deadline.PRESUPUESTO_ORDEN - 5)

    def test_el_finally_restaura_el_contexto_aunque_la_orden_falle(self):
        """
        Sin el `finally`, el presupuesto de esta orden se filtraría a la próxima
        request que atienda el mismo worker de gunicorn.
        """
        with patch("core.services._process_order", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                process_order(1)
        self.assertIsNone(deadline.restante())

    def test_el_finally_restaura_el_contexto_en_el_camino_feliz(self):
        with patch("core.services._process_order", return_value={"status": "ok"}):
            process_order(1)
        self.assertIsNone(deadline.restante())

    def test_presupuesto_agotado_creando_contacto_graba_failed_order(self):
        """Es el caso central de la spec."""
        with patch("core.services.wc_api.get_order", return_value=self._orden()), patch(
            "core.services.resolve_pos_and_payments", return_value=(6, [])
        ), patch(
            "core.services.resolve_contact_id",
            side_effect=deadline.PresupuestoOrdenAgotado("sin tiempo"),
        ):
            with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                process_order(555)
        fallida = FailedOrder.objects.get(order_id=555)
        self.assertIn("contacto", fallida.message.lower())

    def test_presupuesto_agotado_leyendo_productos_graba_failed_order(self):
        """
        La brecha encontrada el 2026-08-26: `build_sale_products` (que hace un
        `get_product` por ítem) NO estaba envuelta en try/except, así que una
        excepción ahí se escapaba de `process_order` sin registro. Es justo el
        tramo que escala con la cantidad de ítems.
        """
        with patch("core.services.wc_api.get_order", return_value=self._orden(items=3)), patch(
            "core.services.resolve_pos_and_payments", return_value=(6, [])
        ), patch(
            "core.services.resolve_contact_id", return_value=(7, ["cliente@example.com"])
        ), patch(
            "core.services.build_sale_products",
            side_effect=deadline.PresupuestoOrdenAgotado("sin tiempo en el item 2"),
        ):
            with self.assertRaises(deadline.PresupuestoOrdenAgotado):
                process_order(556)
        fallida = FailedOrder.objects.get(order_id=556)
        self.assertEqual(fallida.status, FailedOrder.FAILED)
        self.assertIn("productos", fallida.message.lower())

    def test_un_error_cualquiera_leyendo_productos_tambien_se_registra(self):
        """Bug preexistente: un error de WooCommerce en este tramo también se perdía."""
        with patch("core.services.wc_api.get_order", return_value=self._orden()), patch(
            "core.services.resolve_pos_and_payments", return_value=(6, [])
        ), patch(
            "core.services.resolve_contact_id", return_value=(7, ["cliente@example.com"])
        ), patch(
            "core.services.build_sale_products",
            side_effect=RuntimeError("WooCommerce caído"),
        ):
            with self.assertRaises(RuntimeError):
                process_order(557)
        self.assertTrue(FailedOrder.objects.filter(order_id=557).exists())


class CorrelacionOrdenFacturaTest(TestCase):
    """El `sale_id` y el número de factura de BIMS quedan guardados junto a la orden."""

    def _order(self, total="10000"):
        return {
            "total": total,
            "discount_total": "0",
            "meta_data": [],
            "billing": {},
            "shipping": {},
            "line_items": [
                {
                    "product_id": 162,
                    "variation_id": 0,
                    "quantity": 1,
                    "total": total,
                    "total_tax": "0",
                    "name": "Tazas Pequeñas SC",
                }
            ],
            "fee_lines": [],
        }

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_orden_facturada_guarda_sale_id_e_invoice_number(
        self, mock_wc, mock_bims, _mock_contact
    ):
        from core.services import process_order

        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "500"}
        # BIMS devuelve los dos identificadores; hasta ahora el invoice_number se descartaba.
        mock_bims.create_sale.return_value = (31301, 12000, None)

        process_order(order_id=202707)

        registro = FailedOrder.objects.get(order_id=202707)
        self.assertEqual(registro.status, FailedOrder.COMPLETED)
        # Se guardan como texto: BIMS es laxo con los tipos y no hacemos aritmética con ellos.
        self.assertEqual(registro.bims_sale_id, "31301")
        self.assertEqual(registro.bims_invoice_number, "12000")

    @patch.object(BimsApi, "login", return_value="fake_sid")
    def test_create_sale_devuelve_el_invoice_number_de_la_respuesta(self, _mock_login):
        api = BimsApi()
        respuesta = {
            "status": "ok",
            "data": {"Sale": {"id": 31301, "invoice_number": 12000}},
        }
        with patch.object(api, "_retry_request", return_value=respuesta):
            sale_id, invoice_number, error = api.create_sale(
                contact_id=7,
                sale_products=[],
                posale_id=4,
                sales_payment_methods=[],
                contact_emails="cliente@ejemplo.com",
                order=202707,
            )

        self.assertEqual(sale_id, 31301)
        self.assertEqual(invoice_number, 12000)
        self.assertIsNone(error)

    @patch.object(BimsApi, "login", return_value="fake_sid")
    def test_create_sale_sin_invoice_number_no_rompe(self, _mock_login):
        """Si BIMS deja de mandar el campo, la venta se registra igual sin factura."""
        api = BimsApi()
        respuesta = {"status": "ok", "data": {"Sale": {"id": 31302}}}
        with patch.object(api, "_retry_request", return_value=respuesta):
            sale_id, invoice_number, error = api.create_sale(
                contact_id=7,
                sale_products=[],
                posale_id=4,
                sales_payment_methods=[],
                contact_emails="cliente@ejemplo.com",
                order=202708,
            )

        self.assertEqual(sale_id, 31302)
        self.assertIsNone(invoice_number)
        self.assertIsNone(error)

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_sin_invoice_number_se_guarda_null_y_no_el_texto_none(
        self, mock_wc, mock_bims, _mock_contact
    ):
        from core.services import process_order

        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (31302, None, None)

        process_order(order_id=202708)

        registro = FailedOrder.objects.get(order_id=202708)
        self.assertEqual(registro.bims_sale_id, "31302")
        # `str(None)` daría el texto "None", que se vería como una factura real.
        self.assertIsNone(registro.bims_invoice_number)

    @patch("core.services.sentry_sdk")
    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_orden_rechazada_por_bims_no_guarda_identificadores(
        self, mock_wc, mock_bims, _mock_contact, _mock_sentry
    ):
        """
        Guarda de regresión: una orden que no se facturó no puede quedar con un
        `sale_id`, o el campo dejaría de servir para responder "¿se facturó?".
        """
        from core.services import process_order

        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (None, None, "Rechazado")

        with self.assertRaises(ValueError):
            process_order(order_id=202709)

        registro = FailedOrder.objects.get(order_id=202709)
        self.assertEqual(registro.status, FailedOrder.FAILED)
        self.assertIsNone(registro.bims_sale_id)
        self.assertIsNone(registro.bims_invoice_number)

    # ── Paso 4: la factura también queda visible en WooCommerce ─────────────

    def test_update_order_meta_envia_las_claves_al_endpoint_de_la_orden(self):
        api = WooCommerceAPI()
        respuesta = MagicMock(status_code=200)
        # Woo devuelve la orden entera, con `meta_data` incluida. El mock viejo
        # respondía `{"id": ...}` a secas, que no pasa nunca en la realidad.
        respuesta.json.return_value = {
            "id": 202707,
            "meta_data": [{"key": "_bims_sale_id", "value": "31301"}],
        }

        with patch.object(api, "wcapi") as mock_wcapi:
            mock_wcapi.put.return_value = respuesta
            api.update_order_meta(202707, {"_bims_sale_id": "31301"})

        mock_wcapi.put.assert_called_once_with(
            "orders/202707",
            data={"meta_data": [{"key": "_bims_sale_id", "value": "31301"}]},
        )

    def test_update_order_meta_lanza_si_woo_responde_error(self):
        """El cliente reporta fiel; la política de "no romper" vive en services."""
        api = WooCommerceAPI()
        respuesta = MagicMock(status_code=400)
        respuesta.text = "Bad Request"

        with patch.object(api, "wcapi") as mock_wcapi:
            mock_wcapi.put.return_value = respuesta
            with self.assertRaises(WooCommerceAPI.ServerException):
                api.update_order_meta(202707, {"_bims_sale_id": "31301"})

    def test_update_order_meta_lanza_si_woo_responde_200_sin_persistir(self):
        """
        El caso real del 2026-08-31: la orden 204000 facturó en BIMS, el PUT a
        WooCommerce devolvió **200** y la meta no quedó escrita. Sin verificar la
        respuesta no hay excepción, no hay warning y la falla es invisible: hubo
        que descubrirla comparando órdenes a mano.
        """
        api = WooCommerceAPI()
        respuesta = MagicMock(status_code=200)
        respuesta.json.return_value = {
            "id": 204000,
            "meta_data": [{"key": "_krayin_lead_id", "value": "28147"}],
        }

        with patch.object(api, "wcapi") as mock_wcapi:
            mock_wcapi.put.return_value = respuesta
            with self.assertRaises(WooCommerceAPI.ServerException) as ctx:
                api.update_order_meta(204000, {"_bims_sale_id": "31385"})

        # El mensaje va a `FailedOrder.message` y al log: tiene que decir qué
        # clave no se confirmó, o no sirve para diagnosticar.
        self.assertIn("_bims_sale_id", str(ctx.exception))

    def test_update_order_meta_lanza_si_woo_devuelve_otro_valor(self):
        """Confirmar la clave no alcanza: puede volver con un valor distinto."""
        api = WooCommerceAPI()
        respuesta = MagicMock(status_code=200)
        respuesta.json.return_value = {
            "id": 204000,
            "meta_data": [{"key": "_bims_sale_id", "value": "otro"}],
        }

        with patch.object(api, "wcapi") as mock_wcapi:
            mock_wcapi.put.return_value = respuesta
            with self.assertRaises(WooCommerceAPI.ServerException):
                api.update_order_meta(204000, {"_bims_sale_id": "31385"})

    def test_update_order_meta_acepta_el_valor_confirmado_aunque_cambie_de_tipo(self):
        """
        BIMS y WooCommerce son laxos con los tipos: mandamos texto y Woo puede
        devolver un entero. Eso es persistencia correcta, no una falla.
        """
        api = WooCommerceAPI()
        respuesta = MagicMock(status_code=200)
        respuesta.json.return_value = {
            "id": 204000,
            "meta_data": [{"key": "_bims_sale_id", "value": 31385}],
        }

        with patch.object(api, "wcapi") as mock_wcapi:
            mock_wcapi.put.return_value = respuesta
            cuerpo = api.update_order_meta(204000, {"_bims_sale_id": "31385"})

        self.assertEqual(cuerpo["id"], 204000)

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_orden_facturada_anota_la_factura_en_woo(
        self, mock_wc, mock_bims, _mock_contact
    ):
        from core.services import process_order

        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (31301, 12000, None)

        process_order(order_id=202707)

        mock_wc.update_order_meta.assert_called_once_with(
            202707, {"_bims_sale_id": "31301", "_bims_invoice_number": "12000"}
        )

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_si_falla_la_escritura_en_woo_la_orden_sigue_completada(
        self, mock_wc, mock_bims, _mock_contact
    ):
        """
        El test que más importa. En este punto la factura ya existe en BIMS y ya
        se certificó ante la SET: propagar el error daría un 503, y 5 de esos
        seguidos apagan el webhook `Venta Entrada`.
        """
        from core.services import process_order

        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (31301, 12000, None)
        mock_wc.update_order_meta.side_effect = WooCommerceAPI.ServerException("Woo caído")

        resultado = process_order(order_id=202707)

        self.assertEqual(resultado["status"], "ok")
        registro = FailedOrder.objects.get(order_id=202707)
        self.assertEqual(registro.status, FailedOrder.COMPLETED)
        self.assertEqual(registro.bims_sale_id, "31301")

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_woo_que_responde_200_sin_persistir_deja_rastro_en_el_log(
        self, mock_wc, mock_bims, _mock_contact
    ):
        """
        La cadena completa, que es el entregable: si WooCommerce acepta el PUT
        pero no escribe, tiene que quedar un WARNING que diga qué clave faltó.
        Antes de esta instrumentación el flujo terminaba en 200 sin rastro
        alguno, y la orden 204000 se perdió así.

        Se usa el `update_order_meta` REAL (no un side_effect) para que el test
        cubra el camino entero y no la mentira de un mock.
        """
        from core.services import process_order

        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (31385, 12040, None)

        api = WooCommerceAPI()
        respuesta = MagicMock(status_code=200)
        respuesta.json.return_value = {"id": 202707, "meta_data": []}

        with patch.object(api, "wcapi") as mock_wcapi:
            mock_wcapi.put.return_value = respuesta
            mock_wc.update_order_meta = api.update_order_meta
            with self.assertLogs("core.services", level="WARNING") as registrado:
                resultado = process_order(order_id=202707)

        self.assertIn("_bims_sale_id", "\n".join(registrado.output))
        # Y la venta sigue dándose por buena: ya está facturada y certificada.
        self.assertEqual(resultado["status"], "ok")
        self.assertEqual(
            FailedOrder.objects.get(order_id=202707).status, FailedOrder.COMPLETED
        )

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_presupuesto_agotado_al_anotar_en_woo_no_rompe_la_orden(
        self, mock_wc, mock_bims, _mock_contact
    ):
        from core.services import process_order

        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (31301, 12000, None)
        mock_wc.update_order_meta.side_effect = deadline.PresupuestoOrdenAgotado(
            "sin presupuesto"
        )

        resultado = process_order(order_id=202707)

        self.assertEqual(resultado["status"], "ok")
        self.assertEqual(
            FailedOrder.objects.get(order_id=202707).status, FailedOrder.COMPLETED
        )


class RedaccionCamposSensiblesTest(TestCase):
    """
    BIMS devuelve `Agency.tae_password` en texto plano en cada venta creada, y el
    integrador loguea el cuerpo crudo de las respuestas. El enmascarado anterior
    no lo atrapaba por tres razones: comparaba nombres exactos, era de un solo
    nivel, y no se aplicaba a las respuestas.
    """

    def _respuesta_de_venta(self):
        """La forma real de la respuesta de `POST /sales/` (venta 31301)."""
        return {
            "status": "ok",
            "data": {
                "Sale": {"id": 31301, "invoice_number": 12000},
                "Agency": {
                    "id": 1,
                    "name": "MuCi",
                    "tae_username": "usuario_tae",
                    "tae_password": "la-credencial-que-no-debe-quedar",
                },
            },
        }

    def test_redacta_la_clave_anidada_dos_niveles_abajo(self):
        """El caso real: `data.Agency.tae_password`."""
        redactado = _redactar(self._respuesta_de_venta())

        self.assertEqual(redactado["data"]["Agency"]["tae_password"], REDACTADO)

    def test_no_muta_el_objeto_original(self):
        """
        Crítico: `bims.py` saca el session id del MISMO dict que loguea. Un
        filtro in-place metería un bug de negocio dentro de un arreglo de
        seguridad.
        """
        original = self._respuesta_de_venta()
        _redactar(original)

        self.assertEqual(
            original["data"]["Agency"]["tae_password"], "la-credencial-que-no-debe-quedar"
        )

    def test_deja_intacto_lo_que_no_es_sensible(self):
        redactado = _redactar(self._respuesta_de_venta())

        self.assertEqual(redactado["data"]["Sale"]["id"], 31301)
        self.assertEqual(redactado["data"]["Sale"]["invoice_number"], 12000)
        self.assertEqual(redactado["data"]["Agency"]["name"], "MuCi")

    def test_recorre_listas(self):
        """BIMS devuelve arrays (`SalesProduct`, `SalesPaymentMethod`)."""
        cuerpo = {"data": [{"Item": {"api_key": "secreta"}}, {"Item": {"precio": 100}}]}

        redactado = _redactar(cuerpo)

        self.assertEqual(redactado["data"][0]["Item"]["api_key"], REDACTADO)
        self.assertEqual(redactado["data"][1]["Item"]["precio"], 100)

    def test_matchea_por_subcadena_y_sin_importar_mayusculas(self):
        """
        `tae_password` es la razón por la que el enmascarado viejo falló: no es
        igual a `password`. Y el match tiene que sobrevivir a que BIMS agregue
        mañana otro campo con otro prefijo.
        """
        cuerpo = {
            "tae_password": "x",
            "API_KEY": "x",
            "Token": "x",
            "client_secret": "x",
            "mi_apikey": "x",
        }

        redactado = _redactar(cuerpo)

        for clave in cuerpo:
            self.assertEqual(redactado[clave], REDACTADO, "no redactó %s" % clave)

    def test_no_redacta_claves_parecidas_pero_inocentes(self):
        """`tokenizado` matchea `token`; se acepta a propósito (falso positivo
        barato). Pero un campo sin relación no se toca."""
        redactado = _redactar({"password_expires_at": "2026-01-01", "posale_id": 4})

        self.assertEqual(redactado["posale_id"], 4)
        self.assertEqual(redactado["password_expires_at"], REDACTADO)

    def test_redacta_sobre_texto_crudo(self):
        """
        Los 7 sitios que loguean `.text` ya son string: un walker de estructuras
        no sirve ahí.
        """
        crudo = '{"Agency":{"tae_password":"la-credencial","name":"MuCi"}}'

        redactado = _redactar_texto(crudo)

        self.assertNotIn("la-credencial", redactado)
        self.assertIn("MuCi", redactado)

    def test_redactar_texto_tolera_lo_que_no_es_json(self):
        """Esos sitios existen justamente porque la respuesta no era JSON."""
        self.assertEqual(_redactar_texto("502 Bad Gateway"), "502 Bad Gateway")
        self.assertEqual(_redactar_texto(""), "")
        self.assertIsNone(_redactar_texto(None))

    # ── Los helpers no sirven de nada si no se aplican en los sitios reales ──

    def _metodo_que_responde(self, status_code, cuerpo):
        respuesta = requests.Response()
        respuesta.status_code = status_code
        if isinstance(cuerpo, str):
            respuesta._content = cuerpo.encode("utf-8")
        else:
            respuesta._content = json.dumps(cuerpo).encode("utf-8")
        metodo = MagicMock(return_value=respuesta)
        metodo.__name__ = "post"
        return metodo

    @patch.object(BimsApi, "login", return_value="fake_sid")
    @patch("core.bims.bims_logger")
    def test_el_cuerpo_de_la_respuesta_se_loguea_redactado(self, mock_logger, _mock_login):
        api = BimsApi()
        metodo = self._metodo_que_responde(200, self._respuesta_de_venta())

        api._request_with_relogin(metodo, "https://bims.example/sales/", json={"Sale": {}})

        logueado = " ".join(str(l) for l in mock_logger.info.call_args_list)
        self.assertNotIn("la-credencial-que-no-debe-quedar", logueado)
        self.assertIn(REDACTADO, logueado)

    @patch.object(BimsApi, "login", return_value="fake_sid")
    @patch("core.bims.bims_logger")
    def test_una_respuesta_no_json_tambien_se_loguea_redactada(
        self, mock_logger, _mock_login
    ):
        """Los sitios que loguean `res.text` crudo, donde no hay estructura."""
        api = BimsApi()
        crudo = 'ERROR <html> "tae_password":"la-credencial-que-no-debe-quedar" </html>'
        metodo = self._metodo_que_responde(200, crudo)

        with self.assertRaises(Exception):
            api._request_with_relogin(metodo, "https://bims.example/sales/", json={})

        todo = " ".join(
            str(l)
            for l in mock_logger.info.call_args_list
            + mock_logger.error.call_args_list
            + mock_logger.warning.call_args_list
        )
        self.assertNotIn("la-credencial-que-no-debe-quedar", todo)

    @patch.object(BimsApi, "login", return_value="fake_sid")
    @patch("core.bims.bims_logger")
    def test_el_mensaje_de_la_excepcion_sale_redactado(self, _mock_logger, _mock_login):
        """
        `error_msg` no va solo al log: termina en `FailedOrder.message` (base de
        datos) y en Sentry como mensaje de la excepción.
        """
        api = BimsApi()
        metodo = self._metodo_que_responde(
            500, {"Agency": {"tae_password": "la-credencial-que-no-debe-quedar"}}
        )

        with self.assertRaises(BimsTransientError) as ctx:
            api._request_with_relogin(metodo, "https://bims.example/sales/", json={})

        self.assertNotIn("la-credencial-que-no-debe-quedar", str(ctx.exception))


class SettingsRealTest(TestCase):
    """
    El `settings.py` de producción tiene que importar y pasar `check`.

    La suite corre con `test_settings`, que carga 4 apps y esquiva `drf_yasg`,
    `corsheaders` y el admin. Este test cubre el settings real, que es donde
    vive el riesgo de un upgrade de Django: un `ImproperlyConfigured` ahí se
    manifiesta al ARRANCAR el servicio, no al correr los tests.
    """

    def test_el_settings_de_produccion_pasa_check(self):
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        raiz = Path(__file__).resolve().parent.parent

        # El settings hace `dotenv_values(".env")` relativo al cwd, así que se
        # corre en un directorio temporal con un .env sintético. Nunca se toca el
        # .env real, que además no existe en local.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text(
                "SECRET_KEY=solo-para-el-check\n"
                "DEBUG=False\n"
                "ALLOWED_HOSTS=localhost\n"
                "DB_NAME=no_se_conecta\n"
                "DB_USER=no_se_conecta\n"
                "DB_PASSWORD=no_se_conecta\n"
                "DB_HOST=127.0.0.1\n"
                "DB_PORT=3306\n"
                "WOOCOMMERCE_URL=http://test.local\n"
                "WOOCOMMERCE_KEY=k\n"
                "WOOCOMMERCE_SECRET=s\n"
                "BIMS_URL=http://bims.test.local\n"
                "BIMS_USER=u\n"
                "BIMS_PASSWORD=p\n",
                encoding="utf-8",
            )

            codigo = (
                # `core/bims.py` instancia `BimsApi()` al importarse y eso intenta
                # un login REAL contra BIMS; el `check` carga el admin, que
                # arrastra esa cadena. Se corta la red antes de `django.setup()`,
                # igual que hace el encabezado de este archivo.
                "from unittest.mock import patch\n"
                "patch('requests.post').start()\n"
                "patch('requests.Session.request').start()\n"
                "import os, django\n"
                "os.environ['DJANGO_SETTINGS_MODULE'] = 'muci-integrador.settings'\n"
                "django.setup()\n"
                # Sin esto el check queda registrado como cliente de producción en Sentry.
                "import sentry_sdk; sentry_sdk.get_global_scope().set_client(None)\n"
                "from django.core.management import call_command\n"
                "call_command('check')\n"
            )
            entorno = dict(os.environ, PYTHONPATH=str(raiz))
            resultado = subprocess.run(
                [sys.executable, "-c", codigo],
                cwd=tmp,
                env=entorno,
                capture_output=True,
                text=True,
                timeout=120,
            )

        self.assertEqual(
            resultado.returncode,
            0,
            "el settings real no pasa `check`:\n%s\n%s"
            % (resultado.stdout, resultado.stderr),
        )


class SmokeUrlsTest(TestCase):
    """
    Las dos rutas que la suite nunca tocó y que un upgrade de Django rompe
    fácil: la generación del schema de Swagger y el changelist del admin.
    """

    def test_el_schema_de_swagger_se_genera(self):
        """
        `drf_yasg` puede importar y fallar igual al RECORRER las vistas para
        armar el schema. Esto lo ejercita de verdad.
        """
        respuesta = self.client.get("/swagger.json")

        self.assertEqual(respuesta.status_code, 200, respuesta.content[:400])
        self.assertEqual(respuesta["Content-Type"].split(";")[0], "application/json")

        # Un 200 con `{}` pasaría las dos aserciones de arriba sin haber generado
        # nada. Lo que prueba que el recorrido de las vistas funcionó es que haya
        # paths documentados.
        schema = json.loads(respuesta.content)
        self.assertIn("paths", schema)
        self.assertGreater(len(schema["paths"]), 0, "el schema salió sin paths")

    def test_el_changelist_del_admin_de_ordenes_responde(self):
        """
        `FailedOrderAdmin` ganó `bims_sale_id` y `bims_invoice_number` en
        list_display y search_fields el 2026-08-28, sin cobertura. Un nombre de
        campo mal escrito ahí es un error 500 en la pantalla que usa la caja.
        """
        from django.contrib.auth.models import User

        User.objects.create_superuser("admin-test", "admin@test.local", "clave-de-test")
        self.client.login(username="admin-test", password="clave-de-test")

        FailedOrder.objects.create(
            order_id=202707,
            status=FailedOrder.COMPLETED,
            message="Procesado con éxito.",
            bims_sale_id="31301",
            bims_invoice_number="12000",
        )

        respuesta = self.client.get("/admin/core/failedorder/")
        self.assertEqual(respuesta.status_code, 200, respuesta.content[:400])

        # La búsqueda por número de factura es la razón de ser del campo.
        busqueda = self.client.get("/admin/core/failedorder/?q=12000")
        self.assertEqual(busqueda.status_code, 200, busqueda.content[:400])
        self.assertContains(busqueda, "202707")


# ── Sub-proyecto A: ingreso, cola y estado por rama ────────────────────────


class EstadosDeColaTest(TestCase):
    """
    `FailedOrder` pasa a ser cola además de tabla de estado. Los estados nuevos
    se agregan ARRIBA de los existentes, nunca renumerando: hay 8702 filas en
    producción que dependen de que FAILED sea 1 y COMPLETED sea 2.
    """

    def test_los_estados_existentes_conservan_su_valor(self):
        self.assertEqual(FailedOrder.FAILED, 1)
        self.assertEqual(FailedOrder.COMPLETED, 2)

    def test_los_estados_nuevos_existen_con_sus_valores(self):
        self.assertEqual(FailedOrder.PENDING, 3)
        self.assertEqual(FailedOrder.PROCESSING, 4)
        self.assertEqual(FailedOrder.PAUSED, 5)
        self.assertEqual(FailedOrder.NOT_APPLICABLE, 6)

    def test_los_campos_de_cola_tienen_defaults_seguros(self):
        """
        Los defaults importan: la migración los aplica a las 8702 filas
        existentes, así que un default equivocado toca datos fiscales.
        """
        fila = FailedOrder.objects.create(order_id=1)

        self.assertEqual(fila.origin, FailedOrder.ORIGIN_WOO)
        self.assertEqual(fila.bims_attempts, 0)
        self.assertIsNone(fila.bims_next_attempt)
        self.assertFalse(fila.woo_meta_ok)
        self.assertIsNone(fila.claimed_at)


class IdentidadPorOrigenTest(TestCase):
    """
    Fase de EXPANSIÓN de la identidad: se agrega `external_reference` y
    `order_id` queda intacto. La unicidad pasa a ser por (origen, referencia),
    porque el mismo número puede existir en WooCommerce y en el CRM sin ser la
    misma transacción.
    """

    def test_la_misma_referencia_en_origenes_distintos_convive(self):
        FailedOrder.objects.create(
            order_id=204000, external_reference="204000", origin=FailedOrder.ORIGIN_WOO
        )
        FailedOrder.objects.create(
            order_id=204000, external_reference="204000", origin=FailedOrder.ORIGIN_CRM
        )

        self.assertEqual(FailedOrder.objects.count(), 2)

    def test_la_misma_referencia_en_el_mismo_origen_no_se_duplica(self):
        FailedOrder.objects.create(
            order_id=204000, external_reference="204000", origin=FailedOrder.ORIGIN_WOO
        )

        # El `atomic` interno es necesario: sin él la IntegrityError deja la
        # transacción del TestCase inutilizable para cualquier query posterior.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                FailedOrder.objects.create(
                    order_id=204000,
                    external_reference="204000",
                    origin=FailedOrder.ORIGIN_WOO,
                )

    def test_escribir_por_el_helper_llena_las_dos_columnas(self):
        """
        Durante la expansión las dos conviven: `order_id` es la fuente de verdad
        heredada y `external_reference` la nueva. Escribir solo una dejaría
        filas que el código viejo o el nuevo no puede encontrar.
        """
        from core.states import upsert_state

        fila = upsert_state("204000", status=FailedOrder.PENDING)

        self.assertEqual(fila.order_id, 204000)
        self.assertEqual(fila.external_reference, "204000")

    def test_el_helper_actualiza_la_fila_existente_en_vez_de_duplicarla(self):
        from core.states import upsert_state

        upsert_state("204000", status=FailedOrder.PENDING)
        fila = upsert_state("204000", status=FailedOrder.COMPLETED, message="listo")

        self.assertEqual(FailedOrder.objects.count(), 1)
        self.assertEqual(fila.status, FailedOrder.COMPLETED)
        self.assertEqual(fila.message, "listo")

    def test_el_helper_rescata_la_fila_que_dejo_el_codigo_viejo(self):
        """
        En el despliegue, `migrate` corre con el código viejo todavía sirviendo:
        una venta que entre entre el fin de la migración y el `restart` deja una
        fila con `external_reference` en NULL. El helper tiene que adoptarla, no
        crear una segunda fila para la misma orden.
        """
        from core.states import upsert_state

        FailedOrder.objects.create(
            order_id=204000, external_reference=None, status=FailedOrder.FAILED
        )

        fila = upsert_state("204000", status=FailedOrder.COMPLETED)

        self.assertEqual(FailedOrder.objects.count(), 1)
        self.assertEqual(fila.external_reference, "204000")
        self.assertEqual(fila.status, FailedOrder.COMPLETED)

    def test_el_helper_rechaza_una_referencia_no_numerica(self):
        """
        Mientras `order_id` siga siendo NOT NULL no se puede persistir una
        referencia del CRM. Que falle acá y no con un IntegrityError opaco.
        """
        from core.states import upsert_state

        with self.assertRaises(ValueError):
            upsert_state("KRAYIN-77")

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_una_orden_facturada_deja_las_dos_columnas_llenas(
        self, mock_wc, mock_bims, _mock_contact
    ):
        """
        End-to-end sobre `process_order`, no sobre el helper: es el test que
        detecta un call site de `services.py` que quedó escribiendo solo
        `order_id`. Sin esto, migrar seis de siete llamadas pasa desapercibido.
        """
        mock_wc.get_order.return_value = {
            "total": "10000",
            "discount_total": "0",
            "meta_data": [],
            "billing": {},
            "shipping": {},
            "line_items": [
                {
                    "product_id": 162,
                    "variation_id": 0,
                    "quantity": 1,
                    "total": "10000",
                    "total_tax": "0",
                    "name": "Tazas Pequeñas SC",
                }
            ],
            "fee_lines": [],
        }
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_wc.update_order_meta.return_value = None
        mock_bims.create_sale.return_value = (31301, 12000, None)

        process_order(order_id=204000)

        fila = FailedOrder.objects.get(external_reference="204000")
        self.assertEqual(fila.order_id, 204000)
        self.assertEqual(fila.status, FailedOrder.COMPLETED)


class NoAplicaTest(TestCase):
    """
    Las transacciones que no corresponde facturar dejan fila en `NOT_APPLICABLE`.

    Hasta ahora salían por un `return` temprano **sin dejar rastro**, y esa
    ausencia era ambigua: una orden sin `_bims_sale_id` en WooCommerce podía ser
    "no correspondía facturar" o "se perdió en el camino". Con el CRM entrando
    como segundo origen, esa ambigüedad se vuelve una respuesta equivocada a la
    única pregunta que el CRM va a hacer. Ver spec §1.3.
    """

    def _order(self, total="0", discount_total="0", line_items=None, fee_lines=None):
        return {
            "total": total,
            "discount_total": discount_total,
            "meta_data": [],
            "billing": {},
            "shipping": {},
            "line_items": line_items
            if line_items is not None
            else [
                {
                    "product_id": 162,
                    "variation_id": 0,
                    "quantity": 1,
                    "total": total,
                    "total_tax": "0",
                    "name": "Entrada",
                }
            ],
            "fee_lines": fee_lines or [],
        }

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_una_orden_de_monto_cero_deja_fila_en_no_aplica(
        self, mock_wc, mock_bims, _mock_contact
    ):
        mock_wc.get_order.return_value = self._order(total="0")
        mock_wc.get_product.return_value = {"sku": "500"}

        process_order(order_id=202707)

        fila = FailedOrder.objects.get(external_reference="202707")
        self.assertEqual(fila.status, FailedOrder.NOT_APPLICABLE)
        self.assertIn("Monto 0", fila.message)
        mock_bims.create_sale.assert_not_called()

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_un_descuento_del_cien_por_ciento_se_distingue_del_monto_cero(
        self, mock_wc, mock_bims, _mock_contact
    ):
        """
        El motivo va en el mensaje: una entrada gratis de origen y una con
        descuento total son dos decisiones comerciales distintas, y quien mire
        la pantalla necesita poder separarlas.
        """
        mock_wc.get_order.return_value = self._order(total="0", discount_total="40000")
        mock_wc.get_product.return_value = {"sku": "500"}

        process_order(order_id=202708)

        fila = FailedOrder.objects.get(external_reference="202708")
        self.assertEqual(fila.status, FailedOrder.NOT_APPLICABLE)
        self.assertIn("Descuento 100%", fila.message)

    @patch("core.services.resolve_pos_and_payments", return_value=None)
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_una_orden_sin_punto_de_venta_resoluble_deja_fila_en_no_aplica(
        self, mock_wc, mock_bims, _mock_pos
    ):
        mock_wc.get_order.return_value = self._order(total="40000")
        mock_wc.get_product.return_value = {"sku": "500"}

        process_order(order_id=202709)

        fila = FailedOrder.objects.get(external_reference="202709")
        self.assertEqual(fila.status, FailedOrder.NOT_APPLICABLE)
        self.assertIn("No procesado", fila.message)
        mock_bims.create_sale.assert_not_called()

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_todos_los_productos_en_cero_deja_fila_en_no_aplica(
        self, mock_wc, mock_bims, _mock_contact
    ):
        mock_wc.get_order.return_value = self._order(
            total="10000",
            line_items=[
                {
                    "product_id": 1,
                    "variation_id": 0,
                    "quantity": 1,
                    "total": "0",
                    "total_tax": "0",
                    "name": "Merch",
                }
            ],
        )
        mock_wc.get_product.return_value = {"sku": "500"}

        process_order(order_id=202710)

        fila = FailedOrder.objects.get(external_reference="202710")
        self.assertEqual(fila.status, FailedOrder.NOT_APPLICABLE)
        self.assertIn("Productos en 0", fila.message)
        mock_bims.create_sale.assert_not_called()

    def test_no_aplica_llena_las_dos_columnas_de_identidad(self):
        """
        `order_id` es NOT NULL durante la expansión: un helper que escriba solo
        `external_reference` da IntegrityError al crear. Este test lo fija.
        """
        from core.states import mark_not_applicable

        mark_not_applicable(202711, "Monto 0")

        fila = FailedOrder.objects.get(external_reference="202711")
        self.assertEqual(fila.order_id, 202711)
        self.assertEqual(fila.status, FailedOrder.NOT_APPLICABLE)

    def test_no_aplica_es_terminal_y_saca_la_fila_de_la_cola_de_reintentos(self):
        """
        `retryfaileds` y el admin filtran por FAILED. Una orden que antes quedó
        marcada como fallida y resulta que no correspondía facturar tiene que
        dejar de reintentarse, no quedar girando para siempre.
        """
        from core.states import mark_not_applicable

        FailedOrder.objects.create(
            order_id=202712,
            external_reference="202712",
            status=FailedOrder.FAILED,
            message="Error al crear la venta en BIMS.",
        )

        mark_not_applicable(202712, "Monto 0")

        self.assertEqual(FailedOrder.objects.count(), 1)
        self.assertFalse(
            FailedOrder.objects.filter(status=FailedOrder.FAILED).exists()
        )


class SyncBimsContactsPausadasTest(TestCase):
    """
    El auto-reintento de pausadas pasa a filtrar por ESTADO y no por el texto
    del mensaje. Antes el estado de pausa viajaba en `message`, así que
    reformular una cadena rompía el comando sin que nada avisara.

    Nota: el escritor de `"Pausada: Esperando"` se eliminó el 2026-03-17
    (`96e08b9`), así que hoy no se crean filas nuevas por ese camino. Estos tests
    fijan la semántica igual, porque `PAUSED` va a tener un escritor nuevo.

    Que una fila `PAUSED` efectivamente se reencole lo cubre
    `ReintentosEscribenEnLaColaTest`, junto con los otros tres consumidores que
    dejaron de depender del `200` de `/sales/`.
    """

    @patch("core.management.commands.sync_bims_contacts.bims")
    @patch("requests.post")
    def test_una_fallida_con_el_mensaje_viejo_ya_no_se_reintenta_por_aca(
        self, mock_post, mock_bims
    ):
        """
        Las filas históricas las movió la migración 0012. Una que siga en FAILED
        es un fallo de verdad y le corresponde `retryfaileds`, no este camino.
        """
        from django.core.management import call_command

        mock_bims.get_contacts.return_value = {"data": []}
        FailedOrder.objects.create(
            order_id=301,
            external_reference="301",
            status=FailedOrder.FAILED,
            message="Pausada: Esperando sincronización de BIMS",
        )

        call_command("sync_bims_contacts")

        mock_post.assert_not_called()
        self.assertEqual(
            FailedOrder.objects.get(order_id=301).status, FailedOrder.FAILED
        )


class IngresoAsincronoTest(TestCase):
    """
    `POST /sales/` deja de facturar en línea: persiste y devuelve **202**.

    El motivo es concreto y ya costó un webhook: hoy la vista devuelve **503 ante
    cualquier excepción** y WooCommerce deshabilita un webhook a las 5 respuestas
    no-2xx seguidas. Una caída de BIMS de cinco órdenes apaga `Venta Entrada` y
    la facturación se corta **en silencio**. Así murió `Refund order`, que quedó
    con `failure_count 6`.

    Este endpoint no tenía NINGÚN test antes de esta tarea.

    La clase no mockea nada: la vista dejó de importar `process_order`, así que
    la garantía de que no factura en línea es estructural y no depende de un
    mock. Si alguna vez vuelve a hacer falta mockear algo que termine en
    `Response(data=...)`, el `return_value` es obligatorio — un MagicMock hace
    entrar al encoder de DRF en recursión infinita (~22 GiB). Ver `76b8c82`.
    """

    @patch("requests.post")
    def test_el_ingreso_responde_202_y_no_procesa_nada(self, mock_post):
        r = self.client.post("/sales/", {"arg": 204000}, format="json")

        self.assertEqual(r.status_code, 202)
        # Garantia estructural: la vista ni siquiera importa `process_order`.
        # Esto cubre el camino entero — durante el ingreso no sale una sola
        # peticion, ni a BIMS ni a WooCommerce.
        mock_post.assert_not_called()
        fila = FailedOrder.objects.get(external_reference="204000")
        self.assertEqual(fila.status, FailedOrder.PENDING)
        self.assertEqual(fila.order_id, 204000)

    def test_sin_referencia_sigue_siendo_400(self):
        r = self.client.post("/sales/", {}, format="json")

        self.assertEqual(r.status_code, 400)
        self.assertEqual(FailedOrder.objects.count(), 0)

    def test_una_referencia_no_numerica_es_400_y_no_500(self):
        """
        Un 500 le cuenta a Woo como falla igual que un 503. Una referencia
        malformada es culpa del request, no de un tercero: 400 y no se encola.
        """
        r = self.client.post("/sales/", {"arg": "KRAYIN-77"}, format="json")

        self.assertEqual(r.status_code, 400)
        self.assertEqual(FailedOrder.objects.count(), 0)

    def test_una_reentrega_de_orden_completada_no_la_reencola(self):
        """Ya se facturó: reprocesar es riesgo sin beneficio. Spec §4."""
        FailedOrder.objects.create(
            order_id=204000,
            external_reference="204000",
            status=FailedOrder.COMPLETED,
            bims_sale_id="31385",
        )

        r = self.client.post("/sales/", {"arg": 204000}, format="json")

        self.assertEqual(r.status_code, 202)
        fila = FailedOrder.objects.get(external_reference="204000")
        self.assertEqual(fila.status, FailedOrder.COMPLETED)
        self.assertEqual(fila.bims_sale_id, "31385")

    def test_una_reentrega_de_orden_fallida_la_reencola(self):
        FailedOrder.objects.create(
            order_id=204000,
            external_reference="204000",
            status=FailedOrder.FAILED,
            bims_attempts=4,
        )

        self.client.post("/sales/", {"arg": 204000}, format="json")

        fila = FailedOrder.objects.get(external_reference="204000")
        self.assertEqual(fila.status, FailedOrder.PENDING)
        # El presupuesto de intentos se reinicia: es una entrega nueva, no la
        # continuación de la anterior. Sin esto una orden que ya agotó sus
        # reintentos nunca volvería a intentarse.
        self.assertEqual(fila.bims_attempts, 0)

    def test_una_reentrega_de_no_aplica_la_reencola(self):
        """
        Si a una orden de monto 0 le corrigen el precio, Woo reentrega y esta vez
        sí corresponde facturar. `NOT_APPLICABLE` es terminal para el worker,
        no para una entrega nueva.
        """
        FailedOrder.objects.create(
            order_id=204000,
            external_reference="204000",
            status=FailedOrder.NOT_APPLICABLE,
            message="Monto 0",
        )

        self.client.post("/sales/", {"arg": 204000}, format="json")

        self.assertEqual(
            FailedOrder.objects.get(external_reference="204000").status,
            FailedOrder.PENDING,
        )

    def test_una_reentrega_de_una_ya_encolada_no_la_duplica(self):
        """Woo puede reentregar el mismo webhook: dos filas serían dos verdades."""
        self.client.post("/sales/", {"arg": 204000}, format="json")
        self.client.post("/sales/", {"arg": 204000}, format="json")

        self.assertEqual(FailedOrder.objects.count(), 1)


class ReintentosEscribenEnLaColaTest(TestCase):
    """
    Los cuatro reintentos dejan de hablar HTTP con `/sales/` y escriben en la cola.

    No es un cambio de estilo. Los cuatro chequeaban `status_code == 200`, que con
    el 202 no vuelve a ser cierto nunca: no fallan con error, **dejan de hacer nada
    y no avisan**. Ninguno de los cuatro tenía un test que lo detectara — por eso
    la suite entera seguía en verde con el ingreso ya convertido.

    Cada test afirma `mock_post.assert_not_called()` sobre `requests.post`: es la
    forma directa de decir "este camino ya no depende de una respuesta HTTP".
    """

    def _admin(self):
        from django.contrib.admin.sites import site

        from core.admin import FailedOrderAdmin

        instancia = FailedOrderAdmin(FailedOrder, site)
        instancia.message_user = MagicMock()
        return instancia

    def _request(self):
        from django.test import RequestFactory

        return RequestFactory().post("/")

    @patch("requests.post")
    def test_retryfaileds_reencola_las_fallidas(self, mock_post):
        from django.core.management import call_command

        from core.states import upsert_state

        upsert_state(701, status=FailedOrder.FAILED, bims_attempts=5)

        call_command("retryfaileds")

        fila = FailedOrder.objects.get(external_reference="701")
        self.assertEqual(fila.status, FailedOrder.PENDING)
        self.assertEqual(fila.bims_attempts, 0)
        mock_post.assert_not_called()

    @patch("requests.post")
    def test_retryfaileds_no_toca_lo_que_no_esta_fallido(self, mock_post):
        from django.core.management import call_command

        from core.states import upsert_state

        upsert_state(702, status=FailedOrder.COMPLETED, bims_sale_id="31385")

        call_command("retryfaileds")

        fila = FailedOrder.objects.get(external_reference="702")
        self.assertEqual(fila.status, FailedOrder.COMPLETED)
        self.assertEqual(fila.bims_sale_id, "31385")
        mock_post.assert_not_called()

    @patch("requests.post")
    def test_retryfaileds_reencola_una_fila_heredada_sin_referencia(self, mock_post):
        """
        Una venta que entre entre el `migrate` y el `restart` del despliegue deja
        la fila con `external_reference` en NULL. Si el reintento le pasara ese
        NULL a `enqueue`, reventaría con "referencia no numérica" y la orden se
        quedaría sin reintentar para siempre — justo la falla silenciosa que este
        sub-proyecto viene a eliminar.
        """
        from django.core.management import call_command

        FailedOrder.objects.create(order_id=703, status=FailedOrder.FAILED)

        call_command("retryfaileds")

        fila = FailedOrder.objects.get(order_id=703)
        self.assertEqual(fila.status, FailedOrder.PENDING)
        # La reencolada además completa la identidad que faltaba, en vez de
        # crear una segunda fila para la misma orden.
        self.assertEqual(fila.external_reference, "703")
        self.assertEqual(FailedOrder.objects.count(), 1)
        mock_post.assert_not_called()

    @patch("core.management.commands.sync_bims_contacts.bims")
    @patch("requests.post")
    def test_sync_bims_contacts_reencola_las_pausadas(self, mock_post, mock_bims):
        from django.core.management import call_command

        from core.states import upsert_state

        mock_bims.get_contacts.return_value = {"data": []}
        upsert_state(704, status=FailedOrder.PAUSED)

        call_command("sync_bims_contacts")

        self.assertEqual(
            FailedOrder.objects.get(external_reference="704").status,
            FailedOrder.PENDING,
        )
        mock_post.assert_not_called()

    @patch("requests.post")
    def test_el_boton_del_admin_reencola_y_el_mensaje_dice_la_verdad(self, mock_post):
        """
        El mensaje importa tanto como el código: es un botón que aprieta una
        persona. Si dice "procesadas correctamente" cuando en realidad encoló, la
        pantalla miente y alguien va a concluir que el reintento no sirve.
        """
        from core.states import upsert_state

        upsert_state(705, status=FailedOrder.FAILED)
        admin_obj = self._admin()

        admin_obj.retry_failed_orders_button(self._request())

        self.assertEqual(
            FailedOrder.objects.get(external_reference="705").status,
            FailedOrder.PENDING,
        )
        mock_post.assert_not_called()
        mensaje = admin_obj.message_user.call_args[0][1].lower()
        self.assertIn("encolada", mensaje)
        self.assertNotIn("procesadas correctamente", mensaje)

    @patch("requests.post")
    def test_la_accion_del_admin_solo_reencola_las_fallidas(self, mock_post):
        from core.states import upsert_state

        upsert_state(706, status=FailedOrder.FAILED)
        upsert_state(707, status=FailedOrder.COMPLETED, bims_sale_id="31385")
        admin_obj = self._admin()

        admin_obj.retry_selected_orders(self._request(), FailedOrder.objects.all())

        self.assertEqual(
            FailedOrder.objects.get(external_reference="706").status,
            FailedOrder.PENDING,
        )
        self.assertEqual(
            FailedOrder.objects.get(external_reference="707").status,
            FailedOrder.COMPLETED,
        )
        mock_post.assert_not_called()


class WorkerDeColaTest(TestCase):
    """
    `process_queue` es lo que convierte el 202 del ingreso en una factura.

    Sin este comando la Tarea 5 es una cola que nadie vacía, y por eso las dos no
    se pueden desplegar por separado.

    El reaper corre PRIMERO y en la misma pasada: si un worker murió a mitad de
    camino su fila quedó en `PROCESSING` para siempre. Reprocesar es seguro
    porque **BIMS deduplica por `_id`**; sin esa garantía, un reaper sobre datos
    fiscales sería inaceptable.
    """

    def setUp(self):
        """
        Ninguna prueba de esta clase debe salir a la red.

        Desde la Tarea 7 `process_queue` cierra cada pasada reparando las metas
        de WooCommerce, y varias de estas pruebas dejan filas `COMPLETED` con
        `bims_sale_id` para verificar que el worker NO las toma. Esas filas caen
        justo en el filtro de la reparación: sin este parche el comando hacía un
        GET real a WooCommerce, que sólo pasaba desapercibido porque el fallo de
        DNS lo tragaba el `except` del lote.
        """
        parche = patch("core.management.commands.process_queue.wc_api")
        self.mock_wc = parche.start()
        self.addCleanup(parche.stop)
        # Woo ya tiene la meta: la reparación no escribe nada y no distrae.
        self.mock_wc.get_order.return_value = {
            "meta_data": [{"key": "_bims_sale_id", "value": "31385"}]
        }

    def _pendiente(self, referencia, **campos):
        from core.states import upsert_state

        return upsert_state(referencia, status=FailedOrder.PENDING, **campos)

    @patch("core.management.commands.process_queue.process_order")
    def test_procesa_las_pendientes_y_no_las_demas(self, mock_process):
        from django.core.management import call_command

        from core.states import upsert_state

        self._pendiente(1)
        upsert_state(2, status=FailedOrder.COMPLETED, bims_sale_id="31385")

        call_command("process_queue")

        mock_process.assert_called_once_with(order_id="1")

    @patch("core.management.commands.process_queue.process_order")
    def test_no_toca_filas_con_proximo_intento_en_el_futuro(self, mock_process):
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils.timezone import now

        self._pendiente(1, bims_next_attempt=now() + timedelta(minutes=30))

        call_command("process_queue")

        mock_process.assert_not_called()
        # Y la deja PENDING: no es un descarte, es un "todavía no".
        self.assertEqual(
            FailedOrder.objects.get(external_reference="1").status,
            FailedOrder.PENDING,
        )

    @patch("core.management.commands.process_queue.process_order")
    def test_procesa_una_fila_cuyo_proximo_intento_ya_paso(self, mock_process):
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils.timezone import now

        self._pendiente(1, bims_next_attempt=now() - timedelta(minutes=1))

        call_command("process_queue")

        mock_process.assert_called_once_with(order_id="1")

    @patch("core.management.commands.process_queue.process_order")
    def test_el_reaper_recupera_una_fila_colgada(self, mock_process):
        """Si un worker muere a mitad, la fila queda PROCESSING para siempre."""
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils.timezone import now

        from core.states import upsert_state

        upsert_state(
            1,
            status=FailedOrder.PROCESSING,
            claimed_at=now() - timedelta(minutes=30),
        )

        call_command("process_queue")

        # Reencolada y procesada en la misma corrida.
        mock_process.assert_called_once_with(order_id="1")

    @patch("core.management.commands.process_queue.process_order")
    def test_el_reaper_no_toca_una_fila_recien_tomada(self, mock_process):
        """
        Sin esta guarda el reaper le robaría la fila a un worker que todavía está
        trabajando, y dos workers llamarían a BIMS por la misma orden a la vez.
        """
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils.timezone import now

        from core.states import upsert_state

        upsert_state(
            1,
            status=FailedOrder.PROCESSING,
            claimed_at=now() - timedelta(seconds=5),
        )

        call_command("process_queue")

        mock_process.assert_not_called()
        self.assertEqual(
            FailedOrder.objects.get(external_reference="1").status,
            FailedOrder.PROCESSING,
        )

    @patch("core.management.commands.process_queue.process_order")
    def test_una_orden_rota_no_frena_el_lote(self, mock_process):
        """
        La que sigue se procesa igual. `process_order` ya dejó la fila rota en su
        estado correcto, así que tragar la excepción acá no pierde información.
        """
        from django.core.management import call_command

        mock_process.side_effect = [RuntimeError("BIMS explotó"), None]
        self._pendiente(1)
        self._pendiente(2)

        call_command("process_queue")

        self.assertEqual(mock_process.call_count, 2)

    @patch("core.management.commands.process_queue.process_order")
    def test_toma_una_fila_heredada_sin_external_reference(self, mock_process):
        """
        Las filas creadas entre el `migrate` y el `restart` tienen la referencia
        en NULL. Pasarle ese NULL a `process_order` dejaría la orden sin facturar
        y sin que nadie se entere.
        """
        from django.core.management import call_command

        FailedOrder.objects.create(order_id=703, status=FailedOrder.PENDING)

        call_command("process_queue")

        mock_process.assert_called_once_with(order_id="703")

    @patch("core.management.commands.process_queue.process_order")
    def test_marca_PROCESSING_lo_que_toma(self, mock_process):
        """
        La fila tiene que quedar tomada ANTES de llamar a BIMS. Si el proceso
        muere en la llamada, el reaper la encuentra; si se marcara después, la
        fila quedaría PENDING y otro worker la tomaría en paralelo.
        """
        from django.core.management import call_command

        vistas = {}

        def espiar(order_id):
            fila = FailedOrder.objects.get(external_reference=order_id)
            vistas["status"] = fila.status
            vistas["claimed_at"] = fila.claimed_at

        mock_process.side_effect = espiar
        self._pendiente(1)

        call_command("process_queue")

        self.assertEqual(vistas["status"], FailedOrder.PROCESSING)
        self.assertIsNotNone(vistas["claimed_at"])


class ReintentosPorRamaTest(TestCase):
    """
    El backoff es sólo para la rama de BIMS. Spec §6.4.

    La rama de Woo no lleva backoff propio: anotar la meta es barato e
    idempotente y le alcanza con reintentarse en cada pasada. La de BIMS no —
    emite una factura electrónica ante la SET— así que un fallo espera, y a los
    cinco intentos deja de insistir y queda `FAILED` para que alguien mire.
    """

    def _pendiente(self, referencia, **campos):
        from core.states import upsert_state

        return upsert_state(referencia, status=FailedOrder.PENDING, **campos)

    @patch(
        "core.management.commands.process_queue.process_order",
        side_effect=ValueError("BIMS caído"),
    )
    def test_un_fallo_agenda_el_proximo_intento_y_devuelve_la_fila_a_la_cola(
        self, _mock_process
    ):
        from django.core.management import call_command

        self._pendiente(1)

        call_command("process_queue")

        fila = FailedOrder.objects.get(external_reference="1")
        self.assertEqual(fila.bims_attempts, 1)
        self.assertIsNotNone(fila.bims_next_attempt)
        # Vuelve a PENDING, no queda tomada: si quedara PROCESSING sólo la
        # rescataría el reaper, diez minutos después y por el camino de la
        # excepción, que es justo lo que el backoff viene a ordenar.
        self.assertEqual(fila.status, FailedOrder.PENDING)
        self.assertIsNone(fila.claimed_at)

    @patch(
        "core.management.commands.process_queue.process_order",
        side_effect=ValueError("BIMS caído"),
    )
    def test_la_espera_crece_con_los_intentos(self, _mock_process):
        """El primer intento es rápido; los siguientes esperan a que alguien arregle BIMS."""
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils.timezone import now

        self._pendiente(1)
        self._pendiente(2, bims_attempts=2)

        antes = now()
        call_command("process_queue")

        primera = FailedOrder.objects.get(external_reference="1")
        tercera = FailedOrder.objects.get(external_reference="2")
        # 1er intento -> 1 minuto; 3er intento -> 15 minutos.
        self.assertLess(primera.bims_next_attempt - antes, timedelta(minutes=2))
        self.assertGreater(tercera.bims_next_attempt - antes, timedelta(minutes=14))

    @patch(
        "core.management.commands.process_queue.process_order",
        side_effect=ValueError("BIMS caído"),
    )
    def test_agotados_los_intentos_queda_FAILED_y_deja_de_reintentar(
        self, _mock_process
    ):
        from django.core.management import call_command

        self._pendiente(1, bims_attempts=4)

        call_command("process_queue")

        fila = FailedOrder.objects.get(external_reference="1")
        self.assertEqual(fila.status, FailedOrder.FAILED)
        self.assertEqual(fila.bims_attempts, 5)

    @patch(
        "core.management.commands.process_queue.process_order",
        side_effect=ValueError("BIMS caído"),
    )
    def test_agenda_el_reintento_tambien_en_una_fila_sin_referencia(
        self, _mock_process
    ):
        """
        Las filas creadas entre el `migrate` y el `restart` tienen
        `external_reference` en NULL, y el worker las toma por `order_id`. Si el
        reintento se buscara sólo por referencia, esas filas quedarían tomadas y
        las rescataría únicamente el reaper — diez minutos tarde y sin contar el
        intento, así que nunca llegarían a `FAILED`.
        """
        from django.core.management import call_command

        FailedOrder.objects.create(
            order_id=7, external_reference=None, status=FailedOrder.PENDING
        )

        call_command("process_queue")

        fila = FailedOrder.objects.get(order_id=7)
        self.assertEqual(fila.bims_attempts, 1)
        self.assertEqual(fila.status, FailedOrder.PENDING)
        self.assertIsNotNone(fila.bims_next_attempt)

    @patch("core.management.commands.process_queue.process_order")
    def test_un_exito_no_agenda_ningun_reintento(self, _mock_process):
        from django.core.management import call_command

        self._pendiente(1)

        call_command("process_queue")

        fila = FailedOrder.objects.get(external_reference="1")
        self.assertEqual(fila.bims_attempts, 0)
        self.assertIsNone(fila.bims_next_attempt)


class ReparacionDeMetaEnWooTest(TestCase):
    """
    La rama de Woo se repara sola, y `woo_meta_ok` es lo que dice si hace falta.

    Medido en producción el 2026-09-02: las 82 filas con `woo_meta_ok=False`
    **ya tenían la meta correcta en WooCommerce**. Nadie ponía el flag en True al
    anotar, así que la cola de reparación crecía una fila por venta para siempre.
    De ahí las dos garantías de acá: `process_order` marca el flag cuando anota,
    y la pasada de reparación **lee antes de escribir** — un PUT de más dispara
    `order.updated`, que despierta al bot de WhatsApp por una orden vieja.
    """

    def _order(self, total="10000"):
        return {
            "total": total,
            "discount_total": "0",
            "meta_data": [],
            "billing": {},
            "shipping": {},
            "line_items": [
                {
                    "product_id": 162,
                    "variation_id": 0,
                    "quantity": 1,
                    "total": total,
                    "total_tax": "0",
                    "name": "Tazas Pequeñas SC",
                }
            ],
            "fee_lines": [],
        }

    # --- el flag se marca al facturar (core/services.py) ---

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_al_anotar_la_meta_en_woo_marca_woo_meta_ok(
        self, mock_wc, mock_bims, _mock_contact
    ):
        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (31301, 12000, None)

        process_order(order_id=202707)

        fila = FailedOrder.objects.get(order_id=202707)
        self.assertTrue(fila.woo_meta_ok)

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_si_no_se_pudo_anotar_en_woo_el_flag_queda_en_falso(
        self, mock_wc, mock_bims, _mock_contact
    ):
        """La venta igual quedó facturada: el flag marca la deuda, no un fracaso."""
        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "500"}
        mock_bims.create_sale.return_value = (31301, 12000, None)
        mock_wc.update_order_meta.side_effect = requests.RequestException("Woo caído")

        process_order(order_id=202707)

        fila = FailedOrder.objects.get(order_id=202707)
        self.assertEqual(fila.status, FailedOrder.COMPLETED)
        self.assertFalse(fila.woo_meta_ok)

    # --- la pasada de reparación (core/management/commands/process_queue.py) ---

    def _facturada_sin_anotar(self, referencia="204000"):
        from core.states import upsert_state

        return upsert_state(
            referencia,
            status=FailedOrder.COMPLETED,
            bims_sale_id="31385",
            bims_invoice_number="12040",
            woo_meta_ok=False,
        )

    @patch("core.management.commands.process_queue.wc_api")
    @patch("core.management.commands.process_queue.process_order")
    def test_una_venta_facturada_sin_meta_se_repara_en_la_pasada_siguiente(
        self, _mock_process, mock_wc
    ):
        """El caso 204000: facturó, la meta no quedó, y hasta ahora se perdía."""
        from django.core.management import call_command

        self._facturada_sin_anotar()
        # WooCommerce no tiene la meta: la reparación corresponde.
        mock_wc.get_order.return_value = {"meta_data": []}

        call_command("process_queue")

        mock_wc.update_order_meta.assert_called_once_with(
            "204000",
            {"_bims_sale_id": "31385", "_bims_invoice_number": "12040"},
        )
        self.assertTrue(
            FailedOrder.objects.get(external_reference="204000").woo_meta_ok
        )

    @patch("core.management.commands.process_queue.wc_api")
    @patch("core.management.commands.process_queue.process_order")
    def test_si_la_meta_ya_esta_en_woo_no_se_reescribe_y_solo_se_marca_el_flag(
        self, _mock_process, mock_wc
    ):
        """
        Las 82 filas heredadas están en este caso. Reescribirlas serían 82 PUT
        inútiles, y cada uno dispara `order.updated` y despierta al bot.
        """
        from django.core.management import call_command

        self._facturada_sin_anotar()
        mock_wc.get_order.return_value = {
            "meta_data": [
                {"key": "_bims_sale_id", "value": "31385"},
                {"key": "_bims_invoice_number", "value": "12040"},
            ]
        }

        call_command("process_queue")

        mock_wc.update_order_meta.assert_not_called()
        self.assertTrue(
            FailedOrder.objects.get(external_reference="204000").woo_meta_ok
        )

    @patch("core.management.commands.process_queue.wc_api")
    @patch("core.management.commands.process_queue.process_order")
    def test_si_la_reparacion_falla_la_fila_sigue_pendiente_de_anotar(
        self, _mock_process, mock_wc
    ):
        from django.core.management import call_command

        self._facturada_sin_anotar()
        mock_wc.get_order.return_value = {"meta_data": []}
        mock_wc.update_order_meta.side_effect = requests.RequestException("Woo caído")

        call_command("process_queue")

        self.assertFalse(
            FailedOrder.objects.get(external_reference="204000").woo_meta_ok
        )

    @patch("core.management.commands.process_queue.wc_api")
    @patch("core.management.commands.process_queue.process_order")
    def test_no_intenta_reparar_una_venta_sin_sale_id(self, _mock_process, mock_wc):
        """Sin `bims_sale_id` no hay nada que anotar: no tenemos el número."""
        from django.core.management import call_command

        from core.states import upsert_state

        upsert_state("204001", status=FailedOrder.COMPLETED, woo_meta_ok=False)

        call_command("process_queue")

        mock_wc.get_order.assert_not_called()
        mock_wc.update_order_meta.assert_not_called()


class AlertasSlackTest(TestCase):
    """
    Sin esta pieza, el sub-proyecto A **empeora** el sistema: cambia una falla
    ruidosa (el webhook de Woo apagándose) por una invisible (la cola creciendo
    sin que nadie mire). Spec §7.3.

    Sentry queda para bugs de código; Slack para transacciones que no llegaron.
    """

    def test_sin_webhook_configurado_no_postea_ni_revienta(self):
        from core.alerts import notify

        with self.settings(SLACK_WEBHOOK_URL=""):
            with patch("core.alerts.requests.post") as mock_post:
                notify("cola_larga", "hola")

        mock_post.assert_not_called()

    def test_avisa_a_slack_con_el_texto(self):
        from core.alerts import notify

        with self.settings(SLACK_WEBHOOK_URL="https://hooks.slack.test/x"):
            with patch("core.alerts.requests.post") as mock_post:
                notify("cola_larga", "La cola tiene 15 pendientes")

        mock_post.assert_called_once()
        self.assertEqual(
            mock_post.call_args[1]["json"]["text"], "La cola tiene 15 pendientes"
        )
        self.assertIn("timeout", mock_post.call_args[1])

    def test_el_throttle_sobrevive_a_un_proceso_nuevo(self):
        """
        El throttle NO puede vivir en memoria. El worker es un proceso nuevo cada
        minuto (cron), así que un `cache.add` con `LocMemCache` —que es lo que
        hay configurado— arrancaría vacío en cada corrida y avisaría 60 veces por
        hora durante una caída de BIMS, que es justo lo que viene a evitar.
        """
        from django.core.cache import cache

        from core.alerts import notify

        with self.settings(SLACK_WEBHOOK_URL="https://hooks.slack.test/x"):
            with patch("core.alerts.requests.post") as mock_post:
                notify("cola_larga", "primera")
                # Simula el proceso siguiente del cron: memoria en blanco.
                cache.clear()
                notify("cola_larga", "segunda")

        self.assertEqual(mock_post.call_count, 1)

    def test_claves_distintas_no_se_pisan(self):
        from core.alerts import notify

        with self.settings(SLACK_WEBHOOK_URL="https://hooks.slack.test/x"):
            with patch("core.alerts.requests.post") as mock_post:
                notify("cola_larga", "una")
                notify("reintentos_agotados", "otra")

        self.assertEqual(mock_post.call_count, 2)

    def test_pasado_el_silencio_vuelve_a_avisar(self):
        from datetime import timedelta

        from django.utils.timezone import now

        from core.alerts import THROTTLE_MINUTES, AlertThrottle, notify

        with self.settings(SLACK_WEBHOOK_URL="https://hooks.slack.test/x"):
            with patch("core.alerts.requests.post") as mock_post:
                notify("cola_larga", "una")
                AlertThrottle.objects.filter(clave="cola_larga").update(
                    sent_at=now() - timedelta(minutes=THROTTLE_MINUTES + 1)
                )
                notify("cola_larga", "otra")

        self.assertEqual(mock_post.call_count, 2)

    def test_un_fallo_al_avisar_no_rompe_nada(self):
        """Avisar es best-effort: si Slack se cae, la facturación sigue."""
        from core.alerts import notify

        with self.settings(SLACK_WEBHOOK_URL="https://hooks.slack.test/x"):
            with patch(
                "core.alerts.requests.post",
                side_effect=requests.RequestException("Slack caído"),
            ):
                notify("cola_larga", "hola")  # no debe propagar


class DisparadoresDeAlertaTest(TestCase):
    """Los tres disparadores que el worker evalúa al final de cada pasada."""

    def _pendientes(self, cuantas, **campos):
        from core.states import upsert_state

        for i in range(1, cuantas + 1):
            upsert_state(i, status=FailedOrder.PENDING, **campos)

    def setUp(self):
        parche = patch("core.management.commands.process_queue.wc_api")
        self.mock_wc = parche.start()
        self.addCleanup(parche.stop)
        self.mock_wc.get_order.return_value = {"meta_data": []}

        aviso = patch("core.management.commands.process_queue.notify")
        self.mock_notify = aviso.start()
        self.addCleanup(aviso.stop)

    def _claves_avisadas(self):
        return [c[0][0] for c in self.mock_notify.call_args_list]

    @patch("core.management.commands.process_queue.process_order")
    def test_avisa_cuando_la_cola_pasa_el_umbral(self, _mock_process):
        from django.core.management import call_command

        self._pendientes(15)

        with self.settings(QUEUE_ALERT_THRESHOLD=10):
            call_command("process_queue")

        self.assertIn("cola_larga", self._claves_avisadas())

    @patch("core.management.commands.process_queue.process_order")
    def test_una_cola_corta_no_avisa(self, _mock_process):
        from django.core.management import call_command

        self._pendientes(2)

        with self.settings(QUEUE_ALERT_THRESHOLD=10):
            call_command("process_queue")

        self.assertNotIn("cola_larga", self._claves_avisadas())

    @patch(
        "core.management.commands.process_queue.process_order",
        side_effect=ValueError("BIMS caído"),
    )
    def test_avisa_cuando_una_orden_agota_sus_reintentos(self, _mock_process):
        from django.core.management import call_command

        self._pendientes(1, bims_attempts=4)

        call_command("process_queue")

        self.assertIn("reintentos_agotados", self._claves_avisadas())
        texto = [
            c[0][1]
            for c in self.mock_notify.call_args_list
            if c[0][0] == "reintentos_agotados"
        ][0]
        self.assertIn("1", texto)

    @patch(
        "core.management.commands.process_queue.process_order",
        side_effect=ValueError("BIMS caído"),
    )
    def test_un_fallo_que_todavia_reintenta_no_avisa_de_agotados(self, _mock_process):
        from django.core.management import call_command

        self._pendientes(1)

        call_command("process_queue")

        self.assertNotIn("reintentos_agotados", self._claves_avisadas())

    @patch("core.management.commands.process_queue.process_order")
    def test_avisa_cuando_hay_filas_vencidas_que_no_avanzan(self, _mock_process):
        """
        Detecta que la cola NO avanza, que no es lo mismo que "el cron está
        muerto": si el cron no corre, este código no corre y no avisa nada. Eso
        necesita un latido externo y queda fuera de alcance.
        """
        from datetime import timedelta

        from django.core.management import call_command
        from django.utils.timezone import now

        from core.states import upsert_state

        fila = upsert_state(1, status=FailedOrder.PENDING)
        FailedOrder.objects.filter(pk=fila.pk).update(
            updated_at=now() - timedelta(minutes=30)
        )

        with self.settings(QUEUE_SILENCE_MINUTES=10):
            call_command("process_queue")

        self.assertIn("cola_estancada", self._claves_avisadas())

    @patch("core.management.commands.process_queue.process_order")
    def test_una_fila_recien_encolada_no_dispara_estancamiento(self, _mock_process):
        from django.core.management import call_command

        self._pendientes(1)

        with self.settings(QUEUE_SILENCE_MINUTES=10):
            call_command("process_queue")

        self.assertNotIn("cola_estancada", self._claves_avisadas())


class ReintentosTransitoriosFueraDeSentryTest(TestCase):
    """
    `settings.py` usa `LoggingIntegration(event_level=logging.ERROR)`, así que
    **cada `logger.error()` es un evento de Sentry**. El reintento transitorio de
    `bims.py` loguea uno por intento: una orden lenta generaba 3 o 4 eventos
    indistinguibles de un bug de código, y así se pierde la herramienta.
    """

    @patch("core.bims.time.sleep")
    def test_un_reintento_transitorio_loguea_warning_y_no_error(self, _mock_sleep):
        api = BimsApi.__new__(BimsApi)
        api.session = MagicMock()
        api.base_url = "https://bims.test/api"
        api.fallback_url = None
        api.sid = "sid"
        api.api_key = None

        with patch.object(
            api, "_request_with_relogin", side_effect=BimsTransientError("timeout")
        ):
            with self.assertLogs(level="WARNING") as capturado:
                with self.assertRaises(BimsTransientError):
                    api._retry_loop(
                        api.session.get,
                        "https://bims.test/api/sales/",
                        max_retries=2,
                        retry_delay=0,
                        limite=time.monotonic() + 30,
                    )

        niveles = {r.levelname for r in capturado.records}
        self.assertIn("WARNING", niveles)
        self.assertNotIn("ERROR", niveles)


class SkuABimsIdTest(TestCase):
    """
    El SKU de WooCommerce ES el id de producto de BIMS.

    Comprobado sobre merch real el 2026-09-02: SKU 13 en Woo es
    `JUGUETE MEDIDOR DE ALTURA (SPACE) - SC` id 13 en BIMS, SKU 16 es id 16 y
    SKU 128 es `BOLSAS SC`. Ningún producto de BIMS tiene `code` cargado (0 de
    427), así que el puntero vive del lado de Woo y esta traducción es el único
    vínculo que existe.
    """

    def test_un_sku_numerico_es_el_id_de_bims(self):
        from core.stock import bims_product_id

        self.assertEqual(bims_product_id("128"), 128)

    def test_un_sku_vacio_no_tiene_vinculo(self):
        from core.stock import bims_product_id

        self.assertIsNone(bims_product_id(""))
        self.assertIsNone(bims_product_id(None))

    def test_el_sku_cero_no_tiene_vinculo(self):
        """`0` no es un id de BIMS: es el default de un campo sin llenar."""
        from core.stock import bims_product_id

        self.assertIsNone(bims_product_id("0"))

    def test_un_sku_no_numerico_es_un_producto_dado_de_baja(self):
        """
        Convención de Carlos: al dar de baja un producto se le cambia el SKU
        numérico por `<id>-<n>`, donde n es cuántas veces se dio de baja. No es
        un dato inválido, es un estado, y el que llama decide qué hacer.
        """
        from core.stock import SkuDadoDeBaja, bims_product_id

        for sku in ("7-1", "7-19", "149-5-0", "442-1"):
            with self.assertRaises(SkuDadoDeBaja):
                bims_product_id(sku)

    def test_el_espacio_alrededor_no_molesta(self):
        from core.stock import bims_product_id

        self.assertEqual(bims_product_id(" 128 "), 128)


class FacturarConSkuDadoDeBajaTest(TestCase):
    """
    Al facturar, un producto dado de baja DEBE hacer fallar la orden — decisión
    de Carlos. Lo que cambia es el mensaje: antes quedaba
    `invalid literal for int() with base 10: '149-5-0'`, que no le dice nada a
    quien lo lee desde el admin.
    """

    def _order(self):
        return {
            "total": "10000",
            "discount_total": "0",
            "meta_data": [],
            "billing": {},
            "shipping": {},
            "line_items": [
                {
                    "product_id": 162,
                    "variation_id": 0,
                    "quantity": 1,
                    "total": "10000",
                    "total_tax": "0",
                    "name": "Taza dada de baja",
                }
            ],
            "fee_lines": [],
        }

    @patch("core.services.resolve_contact_id", return_value=(999, None))
    @patch("core.services.bims")
    @patch("core.services.wc_api")
    def test_un_producto_dado_de_baja_hace_fallar_la_orden_con_mensaje_claro(
        self, mock_wc, mock_bims, _mock_contact
    ):
        mock_wc.get_order.return_value = self._order()
        mock_wc.get_product.return_value = {"sku": "149-5-0"}

        with self.assertRaises(Exception) as ctx:
            process_order(order_id=204100)

        self.assertIn("149-5-0", str(ctx.exception))
        self.assertIn("baja", str(ctx.exception).lower())
        mock_bims.create_sale.assert_not_called()


class StockVendibleTest(TestCase):
    """
    El número vendible es la suma de `total` sobre los depósitos habilitados.

    Medido el 2026-09-02: `Product.availability`, que calcula BIMS, **es la suma
    de `total`** — verificado en 4 productos. `total2` trae negativos grandes y
    proporcionales al volumen de venta (−285 en cartas infantiles), o sea es una
    salida acumulada y NO stock disponible.
    """

    def _disponibilidad(self, warehouse_id, total, total2="0"):
        return {
            "Availability": {
                "warehouse_id": str(warehouse_id),
                "total": total,
                "total2": total2,
            },
            "Warehouse": {"id": str(warehouse_id), "name": f"Deposito {warehouse_id}"},
        }

    def test_suma_solo_los_depositos_habilitados(self):
        from core.stock import stock_vendible

        av = [
            self._disponibilidad(6, "57.0000000000000000"),
            self._disponibilidad(7, "14"),
            self._disponibilidad(1, "999"),  # Casa Matriz, NO habilitado
        ]

        self.assertEqual(stock_vendible(av, [6, 7]), 71.0)

    def test_ignora_total2_aunque_traiga_numeros(self):
        """El caso real de `JUGUETE CARTAS INFANTILES SC`: total 57, total2 -285."""
        from core.stock import stock_vendible

        av = [self._disponibilidad(6, "57", total2="-285")]

        self.assertEqual(stock_vendible(av, [6, 7]), 57.0)

    def test_parsea_los_tres_formatos_que_manda_bims(self):
        """BIMS devuelve '0', '0.000000' y '128.0000000000000000' en la misma respuesta."""
        from core.stock import stock_vendible

        av = [
            self._disponibilidad(6, "0"),
            self._disponibilidad(7, "0.000000"),
        ]
        self.assertEqual(stock_vendible(av, [6, 7]), 0.0)

        av = [self._disponibilidad(6, "128.0000000000000000")]
        self.assertEqual(stock_vendible(av, [6, 7]), 128.0)

    def test_un_negativo_no_resta_del_total(self):
        """
        Un depósito en negativo es un desajuste de inventario, no una deuda que
        haya que descontarle a otro depósito. Se trata como 0.
        """
        from core.stock import stock_vendible

        av = [
            self._disponibilidad(6, "10"),
            self._disponibilidad(7, "-4"),
        ]

        self.assertEqual(stock_vendible(av, [6, 7]), 10.0)

    def test_sin_disponibilidades_es_cero(self):
        from core.stock import stock_vendible

        self.assertEqual(stock_vendible(None, [6, 7]), 0.0)
        self.assertEqual(stock_vendible([], [6, 7]), 0.0)

    def test_el_desglose_dice_de_donde_salio_cada_unidad(self):
        """
        Sin esto, un número publicado en la web no se puede explicar: Woo no sabe
        en qué depósito vive nada, sólo BIMS lo sabe.
        """
        from core.stock import desglose_por_deposito

        av = [
            self._disponibilidad(6, "57"),
            self._disponibilidad(7, "14"),
            self._disponibilidad(1, "999"),
        ]

        self.assertEqual(desglose_por_deposito(av, [6, 7]), {6: 57.0, 7: 14.0})

    def test_el_desglose_omite_los_depositos_en_cero(self):
        from core.stock import desglose_por_deposito

        av = [self._disponibilidad(6, "57"), self._disponibilidad(7, "0")]

        self.assertEqual(desglose_por_deposito(av, [6, 7]), {6: 57.0})

    def test_un_total_ilegible_no_rompe_el_barrido(self):
        """Un valor que no parsea cuenta como 0, no tira el barrido entero."""
        from core.stock import stock_vendible

        av = [
            {"Availability": {"warehouse_id": "6", "total": "en revision"}},
            self._disponibilidad(7, "5"),
        ]

        self.assertEqual(stock_vendible(av, [6, 7]), 5.0)


class HerenciaDeSkuTest(TestCase):
    """
    Heredar el SKU del padre es la semántica de WooCommerce
    (`WC_Product_Variation::get_sku()` lo hace), así que no es un parche. Pero se
    rompe con varias hermanas sin SKU: `Taza pequeña` tiene SKU 27 en el padre y
    **9 variaciones sin SKU** —nueve diseños de la misma taza— y en BIMS el
    producto 27 tiene UN stock. Heredar ahí escribiría el mismo número nueve
    veces: inventario multiplicado por 9.

    Medido el 2026-09-02: 323 variaciones tienen SKU propio, 32 padres tienen
    exactamente una variación sin SKU, y 12 padres tienen varias (59
    variaciones, con `Libros Ttklab` en 23).
    """

    def test_el_sku_propio_de_la_variacion_manda(self):
        from core.stock import resolver_bims_id

        bims_id, motivo = resolver_bims_id("13", "999", hermanas_sin_sku=1)

        self.assertEqual(bims_id, 13)
        self.assertIsNone(motivo)

    def test_sin_sku_propio_y_unica_hermana_hereda_del_padre(self):
        from core.stock import resolver_bims_id

        bims_id, motivo = resolver_bims_id(None, "575", hermanas_sin_sku=1)

        self.assertEqual(bims_id, 575)
        self.assertIsNone(motivo)

    def test_con_varias_hermanas_sin_sku_no_se_hereda(self):
        """El caso `Taza pequeña`: heredar multiplicaría el stock por 9."""
        from core.stock import SKU_AMBIGUO, resolver_bims_id

        bims_id, motivo = resolver_bims_id(None, "27", hermanas_sin_sku=9)

        self.assertIsNone(bims_id)
        self.assertEqual(motivo, SKU_AMBIGUO)

    def test_ni_la_variacion_ni_el_padre_tienen_sku(self):
        from core.stock import SKU_SIN_VINCULO, resolver_bims_id

        bims_id, motivo = resolver_bims_id(None, None, hermanas_sin_sku=1)

        self.assertIsNone(bims_id)
        self.assertEqual(motivo, SKU_SIN_VINCULO)

    def test_un_producto_dado_de_baja_se_saltea_sin_frenar_el_barrido(self):
        """
        En el barrido, a diferencia de la facturación, una baja NO es un error:
        se saltea y se cuenta. Frenar un barrido entero porque un producto está
        de baja sería absurdo.
        """
        from core.stock import SKU_DADO_DE_BAJA, resolver_bims_id

        bims_id, motivo = resolver_bims_id("7-19", None, hermanas_sin_sku=1)

        self.assertIsNone(bims_id)
        self.assertEqual(motivo, SKU_DADO_DE_BAJA)

    def test_un_padre_dado_de_baja_no_se_hereda(self):
        from core.stock import SKU_DADO_DE_BAJA, resolver_bims_id

        bims_id, motivo = resolver_bims_id(None, "442-1", hermanas_sin_sku=1)

        self.assertIsNone(bims_id)
        self.assertEqual(motivo, SKU_DADO_DE_BAJA)


class CalculoDeCambiosTest(TestCase):
    """Sólo se escribe donde el número difiere, y no se apaga en masa."""

    def test_solo_devuelve_los_que_cambian(self):
        from core.stock import calcular_cambios

        candidatos = [
            {"woo_id": 100, "ruta_woo": "100", "bims_id": 13, "stock_actual": 5.0},
            {"woo_id": 200, "ruta_woo": "200", "bims_id": 128, "stock_actual": 128.0},
        ]

        cambios = calcular_cambios(candidatos, {13: 7.0, 128: 128.0})

        self.assertEqual(len(cambios), 1)
        self.assertEqual(cambios[0].woo_id, 100)
        self.assertEqual(cambios[0].stock_nuevo, 7.0)

    def test_una_variacion_lleva_la_ruta_anidada(self):
        """WooCommerce escribe una variación en `products/{padre}/variations/{id}`."""
        from core.stock import calcular_cambios

        candidatos = [
            {
                "woo_id": 188079,
                "ruta_woo": "187056/variations/188079",
                "bims_id": 575,
                "stock_actual": 49.0,
            }
        ]

        cambios = calcular_cambios(candidatos, {575: 16.0})

        self.assertEqual(cambios[0].ruta_woo, "187056/variations/188079")

    def test_no_escribe_un_producto_que_bims_no_devolvio(self):
        """
        Si BIMS no trajo ese producto, no hay dato: no se toca. Esta es la guarda
        que evita que una lectura fallida apague el catálogo.
        """
        from core.stock import calcular_cambios

        candidatos = [{"woo_id": 100, "ruta_woo": "100", "bims_id": 13, "stock_actual": 5.0}]

        self.assertEqual(calcular_cambios(candidatos, {}), [])

    def test_marca_los_que_apagan_la_venta(self):
        from core.stock import calcular_cambios

        candidatos = [
            {"woo_id": 100, "ruta_woo": "100", "bims_id": 13, "stock_actual": 5.0},
            {"woo_id": 200, "ruta_woo": "200", "bims_id": 14, "stock_actual": 0.0},
        ]

        cambios = calcular_cambios(candidatos, {13: 0.0, 14: 3.0})

        apagados = [c for c in cambios if c.apaga]
        self.assertEqual([c.woo_id for c in apagados], [100])

    def test_una_diferencia_de_decimales_no_es_un_cambio(self):
        """Woo guarda '5' y BIMS '5.0000000000000000': es el mismo número."""
        from core.stock import calcular_cambios

        candidatos = [{"woo_id": 100, "ruta_woo": "100", "bims_id": 13, "stock_actual": 5.0}]

        self.assertEqual(calcular_cambios(candidatos, {13: 5.0}), [])


class GuardaDeRadioTest(TestCase):
    """
    Si un barrido apagaría más de N productos de golpe, casi siempre es un
    problema de la consulta o del depósito configurado, no que se haya vendido
    todo.

    N = 5 elegido con medición el 2026-09-02: hay 44 productos publicados con
    stock > 0 en Woo (41 con SKU propio), y el ritmo real fue de 3 a 20 productos
    DISTINTOS vendidos por día — pero eso es vendidos, no llegados a cero. En una
    ventana de 15 minutos lo esperable es 0 o 1.
    """

    def _cambio(self, woo_id, apaga):
        from core.stock import Cambio

        return Cambio(
            woo_id=woo_id, ruta_woo=str(woo_id), bims_id=woo_id, stock_actual=1.0,
            stock_nuevo=0.0 if apaga else 9.0, apaga=apaga,
        )

    def test_apagar_pocos_esta_dentro_del_tope(self):
        from core.stock import radio_excedido

        cambios = [self._cambio(i, apaga=True) for i in range(3)]

        self.assertEqual(radio_excedido(cambios, tope=5), 0)

    def test_apagar_mas_que_el_tope_lo_excede(self):
        from core.stock import radio_excedido

        cambios = [self._cambio(i, apaga=True) for i in range(6)]

        self.assertEqual(radio_excedido(cambios, tope=5), 6)

    def test_las_subidas_no_cuentan_para_el_tope(self):
        """El primer barrido va a ENCENDER decenas de productos: eso es correcto."""
        from core.stock import radio_excedido

        cambios = [self._cambio(i, apaga=False) for i in range(40)]

        self.assertEqual(radio_excedido(cambios, tope=5), 0)


class SettingsDeStockTest(TestCase):
    """Los cuatro settings del barrido existen y tienen el tipo correcto."""

    def test_los_settings_existen_con_su_tipo(self):
        from django.conf import settings

        self.assertIsInstance(settings.STOCK_WAREHOUSE_IDS, list)
        self.assertTrue(all(isinstance(d, int) for d in settings.STOCK_WAREHOUSE_IDS))
        self.assertIsInstance(settings.STOCK_ZERO_GUARD, int)
        self.assertIsInstance(settings.STOCK_SYNC_ENABLED, bool)
        self.assertIsInstance(settings.STOCK_PAGE_SIZE, int)

    def test_la_pagina_no_puede_ser_grande(self):
        """
        Medido el 2026-09-02: `products/index.json?v_stock=1` con limit=500 NO
        entra en los 30 s de TIMEOUT_LECTURA y da timeout. Y el reintento hereda
        las sobras del presupuesto de 40 s, así que tampoco salva.
        """
        from django.conf import settings

        self.assertLessEqual(settings.STOCK_PAGE_SIZE, 100)


class LecturaDeStockDeBimsTest(TestCase):
    """
    `bims.py` no tenía NINGUNA lectura de productos: sólo `create_sale`,
    `get_posales`, `get_contacts` y `list_contacts`.
    """

    @patch.object(BimsApi, "login", return_value="fake_sid")
    def test_pide_v_stock_para_que_venga_la_disponibilidad(self, _mock_login):
        """
        Sin `v_stock=1` la respuesta NO trae `AvailabilityFull`, que es de donde
        sale el stock por depósito. Verificado contra la API viva.
        """
        api = BimsApi()
        respuesta = {"status": "ok", "data": []}

        with patch.object(api, "_retry_request", return_value=respuesta) as mock_req:
            api.get_products_with_stock(limit=100, offset=0)

        params = mock_req.call_args[1]["params"]
        self.assertEqual(params["v_stock"], 1)
        self.assertEqual(params["limit"], 100)
        self.assertEqual(params["offset"], 0)

    @patch.object(BimsApi, "login", return_value="fake_sid")
    def test_usa_el_endpoint_de_index_con_json(self, _mock_login):
        api = BimsApi()

        with patch.object(api, "_retry_request", return_value={"status": "ok"}) as mock_req:
            api.get_products_with_stock(limit=100, offset=200)

        url = mock_req.call_args[0][1]
        self.assertTrue(url.endswith("/products/index.json"), url)
