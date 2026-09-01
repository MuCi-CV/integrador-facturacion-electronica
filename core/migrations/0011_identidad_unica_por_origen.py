# La unicidad pasa a ser (origen, referencia). Va en una migracion aparte de la
# 0010 y detras de una guarda, por dos razones:
#
# 1. Hoy `order_id` NO tiene constraint unico y `update_or_create` es
#    competible, asi que puede haber `order_id` repetidos entre las 8702 filas
#    de produccion. Tras el backfill de la 0010 esos duplicados se vuelven
#    `(woo, "204000")` repetidos y el constraint FALLA.
# 2. En MariaDB el DDL hace commit implicito. Si el constraint fallara dentro de
#    la 0010, la columna nueva y el backfill ya estarian aplicados pero la
#    migracion figuraria como no aplicada: reaplicarla explota en el AddField
#    ("column already exists") y hay que desenredarla a mano.
#
# Con la guarda como PRIMERA operacion, una base con duplicados no llega a
# ningun DDL: la 0011 aborta entera y el esquema queda consistente en la 0010.

from django.db import migrations
from django.db.models import Count


def abortar_si_hay_referencias_duplicadas(apps, schema_editor):
    """
    Falla ANTES de cualquier DDL si los datos no admiten el constraint.

    Un `IntegrityError` crudo de MariaDB dice que la clave duplicada es
    'woo-204000' y nada mas. Este mensaje dice cuales son y cuantas, que es lo
    que hace falta para decidir si se deduplica o se cambia el diseno.
    """
    FailedOrder = apps.get_model("core", "FailedOrder")
    duplicadas = (
        FailedOrder.objects.exclude(external_reference__isnull=True)
        .values("origin", "external_reference")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .order_by("-n")
    )
    muestra = list(duplicadas[:10])
    if not muestra:
        return

    total = duplicadas.count()
    detalle = ", ".join(
        f"({d['origin']}, {d['external_reference']}) x{d['n']}" for d in muestra
    )
    raise RuntimeError(
        f"No se puede aplicar la unicidad (origin, external_reference): hay "
        f"{total} referencia(s) duplicada(s). Muestra: {detalle}. "
        "Deduplicar antes de reintentar; el esquema quedo consistente en la 0010."
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_external_reference"),
    ]

    operations = [
        migrations.RunPython(
            abortar_si_hay_referencias_duplicadas, migrations.RunPython.noop
        ),
        migrations.AlterUniqueTogether(
            name="failedorder",
            unique_together={("origin", "external_reference")},
        ),
    ]
