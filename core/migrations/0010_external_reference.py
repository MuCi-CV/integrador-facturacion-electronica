# Fase de EXPANSION de la identidad: se agrega `external_reference` y se llena
# por copia. `order_id` queda INTACTO. Borrarlo es una tarea diferida.
#
# Esta migracion es ADITIVA en el sentido que importa: ningun consumidor lee las
# columnas que toca, asi que el comportamiento del sistema no cambia al
# aplicarla, y el rollback es volver el codigo sin tocar la base.
#
# El `unique_together` NO va aca: vive en la 0011, detras de una guarda. En
# MariaDB el DDL hace commit implicito, asi que si el constraint fallara por
# datos duplicados esta migracion quedaria a mitad de camino.

from django.db import migrations, models
from django.db.models.functions import Cast


def llenar_external_reference(apps, schema_editor):
    """
    Copia, no movimiento: `order_id` queda intacto.

    Un UPDATE unico y no un bucle con `.save()`: son 8702 filas en produccion y
    8702 round trips sostenidos contra la base, dentro de una migracion, es
    tiempo de bloqueo que no hace falta pagar.

    Idempotente por el filtro `isnull=True`: si el backfill sale mal se vuelve a
    correr sin pisar lo que ya se lleno.
    """
    FailedOrder = apps.get_model("core", "FailedOrder")
    FailedOrder.objects.filter(external_reference__isnull=True).update(
        external_reference=Cast("order_id", models.CharField(max_length=64))
    )


def vaciar_external_reference(apps, schema_editor):
    apps.get_model("core", "FailedOrder").objects.update(external_reference=None)


def marcar_woo_meta_sin_pendiente(apps, schema_editor):
    """
    `woo_meta_ok=True` donde no queda nada por anotar en WooCommerce.

    El criterio NO es una fecha de corte sino el dato mismo: una fila COMPLETED
    sin `bims_sale_id` **no se puede anotar**, porque no tenemos el numero que
    habria que escribirle a la orden. Son las anteriores al sub-proyecto A'
    (2026-08-28), que es cuando se empezo a guardar ese id.

    El plan pedia cortar por `created_at < 2026-08-28`. Se cambio por esto: no
    depende de adivinar la hora exacta del despliegue de A', y deja el flag
    diciendo la verdad operativa ("no hay trabajo pendiente en esta rama") en vez
    de afirmar que se anoto algo que nunca se anoto.

    Las COMPLETED que SI tienen `bims_sale_id` quedan en False a proposito: son
    pocas (post-A') y el reaper de la Tarea 7 las va a verificar, que es
    exactamente lo que queremos despues del incidente de la orden 204000.
    """
    FailedOrder = apps.get_model("core", "FailedOrder")
    FailedOrder.objects.filter(
        status=2,  # COMPLETED. Literal a proposito: una migracion no debe
        # depender de constantes del modelo, que pueden cambiar despues.
        bims_sale_id__isnull=True,
    ).update(woo_meta_ok=True)


def desmarcar_woo_meta_sin_pendiente(apps, schema_editor):
    FailedOrder = apps.get_model("core", "FailedOrder")
    FailedOrder.objects.filter(status=2, bims_sale_id__isnull=True).update(
        woo_meta_ok=False
    )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_estados_y_campos_de_cola"),
    ]

    operations = [
        migrations.AddField(
            model_name="failedorder",
            name="external_reference",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=64,
                null=True,
                verbose_name="Referencia externa",
            ),
        ),
        migrations.RunPython(llenar_external_reference, vaciar_external_reference),
        migrations.RunPython(
            marcar_woo_meta_sin_pendiente, desmarcar_woo_meta_sin_pendiente
        ),
    ]
