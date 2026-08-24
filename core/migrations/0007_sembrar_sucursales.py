from django.db import migrations

# Estado exacto de `core/constants.py` al momento de mover el mapeo a la BD.
# Los valores van inline a propósito: una migración de datos no debe importar
# código de la app, porque ese código cambia y la migración ya corrió.
#
# Los emails de los dos cajeros se verificaron contra la API de WooCommerce el
# 2026-08-24: customers/729 -> sancosmos@muci.org y customers/3 ->
# tatakuashop@muci.org, ambos con rol fooeventspos_cashier.
SEMILLA = [
    {
        "tipo": "cajero",
        "nombre": "San Cosmos",
        "email": "sancosmos@muci.org",
        "wp_user_id": 729,
        "bims_posale_id": 4,
    },
    {
        "tipo": "cajero",
        "nombre": "Tatakualab",
        "email": "tatakuashop@muci.org",
        "wp_user_id": 3,
        "bims_posale_id": 1,
    },
    {
        # `bims_posale_id` vacío = no facturar. Reemplaza el `if user_id_value == 2`
        # que estaba hardcodeado en services.py.
        "tipo": "cajero",
        "nombre": "Administrador",
        "email": "",
        "wp_user_id": 2,
        "bims_posale_id": None,
    },
    {
        "tipo": "pos_sin_mapeo",
        "nombre": "Cualquier otro cajero POS",
        "email": "",
        "wp_user_id": None,
        "bims_posale_id": 7,
    },
    {
        "tipo": "web",
        "nombre": "Órdenes web",
        "email": "",
        "wp_user_id": None,
        "bims_posale_id": 6,
    },
]


def sembrar(apps, schema_editor):
    """
    Siembra el estado actual para que el comportamiento no cambie el día uno.

    Es idempotente y no pisa nada: si la tabla ya tiene filas, no hace nada, y
    si falta solo alguna, la agrega respetando lo que ya esté cargado.
    """
    Sucursal = apps.get_model("core", "Sucursal")
    for fila in SEMILLA:
        if fila["wp_user_id"] is not None:
            existe = Sucursal.objects.filter(wp_user_id=fila["wp_user_id"]).exists()
        else:
            existe = Sucursal.objects.filter(tipo=fila["tipo"]).exists()
        if not existe:
            Sucursal.objects.create(**fila)


def revertir(apps, schema_editor):
    """
    No borra nada: para cuando se revierta, la tabla puede tener sucursales
    cargadas a mano y no hay forma de distinguirlas de las sembradas. La
    migración de esquema (0006) elimina la tabla completa de todos modos.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_sucursal"),
    ]

    operations = [
        migrations.RunPython(sembrar, revertir),
    ]
