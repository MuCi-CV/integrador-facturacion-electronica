# Estados y campos de cola en FailedOrder. Aditivo: ningun dato se toca.
# El AlterField de 'status' solo amplia los choices, no cambia valores.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_failedorder_bims_invoice_number_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='failedorder',
            name='bims_attempts',
            field=models.PositiveSmallIntegerField(default=0, verbose_name='Intentos contra BIMS'),
        ),
        migrations.AddField(
            model_name='failedorder',
            name='bims_next_attempt',
            field=models.DateTimeField(blank=True, db_index=True, null=True, verbose_name='Próximo intento'),
        ),
        migrations.AddField(
            model_name='failedorder',
            name='claimed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Tomada por el worker'),
        ),
        migrations.AddField(
            model_name='failedorder',
            name='origin',
            field=models.CharField(choices=[('woo', 'WooCommerce'), ('crm', 'CRM Krayin')], db_index=True, default='woo', max_length=8, verbose_name='Origen'),
        ),
        migrations.AddField(
            model_name='failedorder',
            name='woo_meta_ok',
            field=models.BooleanField(default=False, verbose_name='Anotada en WooCommerce'),
        ),
        migrations.AlterField(
            model_name='failedorder',
            name='status',
            field=models.PositiveSmallIntegerField(choices=[(1, 'Fallido'), (2, 'Completado'), (3, 'Pendiente'), (4, 'En proceso'), (5, 'Pausada'), (6, 'No aplica')], db_index=True, default=1, verbose_name='Estado'),
        ),
    ]
