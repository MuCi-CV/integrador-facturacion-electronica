# Las FAILED cuyo `message` empieza con "Pausada: Esperando" pasan al estado
# PAUSED, que es lo que siempre fueron: no fallaron, esperaban algo externo.
#
# POR QUE VIAJA CON EL CAMBIO DE CODIGO Y NO EN LA 0010
#   `sync_bims_contacts.py` filtraba por (FAILED, message startswith "Pausada:
#   Esperando"). Mover los estados sin cambiar ese filtro lo deja sin encontrar
#   nada y **sin avisar** — el mismo modo de falla silenciosa que el 202. Por eso
#   esta migracion y el cambio del filtro son un solo commit.
#
# ES LIMPIEZA HISTORICA, NO UN CANAL VIVO
#   El escritor de ese mensaje se elimino el 2026-03-17 (commit 96e08b9, en
#   core/views.py), asi que desde marzo no se crean filas nuevas asi. La spec §5
#   lo describe como si el canal siguiera activo; no lo esta. Si en produccion no
#   quedan filas con ese mensaje, esta migracion es un no-op, y esta bien: deja
#   el esquema y el codigo coherentes para cuando PAUSED tenga un escritor nuevo.

from django.db import migrations

PREFIJO = "Pausada: Esperando"
FAILED = 1
PAUSED = 5


def a_pausada(apps, schema_editor):
    """Un UPDATE unico: son pocas filas y no hace falta traerlas a Python."""
    FailedOrder = apps.get_model("core", "FailedOrder")
    FailedOrder.objects.filter(status=FAILED, message__startswith=PREFIJO).update(
        status=PAUSED
    )


def a_fallida(apps, schema_editor):
    """
    La inversa se restringe al mismo prefijo: si algun dia hay filas PAUSED
    escritas por otro camino, un rollback no debe convertirlas en fallidas.
    """
    FailedOrder = apps.get_model("core", "FailedOrder")
    FailedOrder.objects.filter(status=PAUSED, message__startswith=PREFIJO).update(
        status=FAILED
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_identidad_unica_por_origen"),
    ]

    operations = [
        migrations.RunPython(a_pausada, a_fallida),
    ]
