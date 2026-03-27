# Mapeo FooEvents POS user_id → BIMS posale_id
# None indica que la orden NO debe procesarse (cuenta de administrador)
POS_USER_ID_TO_POSALE = {
    2: None,  # Administrador — no procesar
    729: 4,   # San Cosmos
    3: 1,     # Tatakualab
}
POS_DEFAULT_POSALE_ID = 7  # Cualquier otro cajero POS
WEB_POSALE_ID = 6          # Órdenes web (sin _fooeventspos_user_id)

# Mapeo FooEvents POS payment key → BIMS payment_method_id
FOOEVENTS_PAYMENT_METHOD_MAP = {
    "fooeventspos_check_payment": 34,     # Gift Card
    "fooeventspos_cash": 21,              # Efectivo
    "fooeventspos_cash_on_delivery": 26,  # Transferencia Bancaria
    "fooeventspos_other": 27,             # Bancard
    "fooeventspos_online": 28,            # Pago en línea
}
PAYMENT_METHOD_DEFAULT_ID = 28  # Fallback: Pago en línea

# IDs de productos WooCommerce con lógica de precio especial
# Usan precio con descuento (total/qty + tax)
DISCOUNT_PRICE_PRODUCT_IDS = frozenset({10648, 14369})

# Usan precio plano (item.total sin dividir por cantidad)
FLAT_PRICE_PRODUCT_IDS = frozenset({
    19657,  # Entrada grupal San Cosmos
    14372,  # Entrada grupal Tatakualab
    8421,   # Donación en caja
    3681,   # Paquete de cumpleaños
    24482,  # Entrada online San Cosmos
})

TIP_BIMS_PRODUCT_ID = 100
