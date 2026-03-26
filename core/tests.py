from unittest.mock import MagicMock, patch

# bims.py instancia BimsApi() al ser importado, lo que intenta conectar a BIMS.
# Mockeamos requests.post antes de importar core.services para evitar esa conexión.
with patch("requests.post") as _mock_post:
    _mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"status": "ok", "data": {"Session": {"id": "mock_sid"}}},
    )
    from core.services import _parse_pos_payments, build_sale_products, resolve_pos_and_payments

from django.test import TestCase


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
