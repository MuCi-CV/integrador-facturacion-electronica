# Generated manually

from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_failedorder_message'),
    ]

    operations = [
        migrations.CreateModel(
            name='ContactCache',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('bims_id', models.IntegerField(unique=True, verbose_name='ID en BIMS')),
                ('email', models.EmailField(db_index=True, max_length=254, verbose_name='Email')),
                ('document_id', models.CharField(blank=True, db_index=True, max_length=50, null=True, verbose_name='Documento')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Caché de Contacto',
                'verbose_name_plural': 'Cachés de Contactos',
            },
        ),
    ]
