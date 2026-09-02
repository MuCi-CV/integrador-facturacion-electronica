"""
RESCATADO del commit `c6a87dd` (2025-11-03), NO APTO PARA PRODUCCIÓN.

Su `runsyncstock.sh` está en el servidor desde ese mismo día y está roto por tres
motivos a la vez: hace `cd` a `/var/www/integrador.muci.org/backend` (ruta que no
existe), activa un `.venv` que ahí no hay, y llama a este comando, que no está en
el código desplegado. Su línea de cron además está comentada.

Los defectos de fondo están listados en `core/stock_sync_rescatado.py`. El más
importante: le pega por HTTP a su propio Django, así que necesita gunicorn vivo
para hacer algo que no necesita web.
"""

from django.core.management.base import BaseCommand
from django.conf import settings
import requests

class Command(BaseCommand):
    help = "Sincroniza stock desde BIMS a WooCommerce"

    def handle(self, *args, **options):
        url = settings.BASE_URL + "/stock-sync/"
        offset = 0
        
        while True:
            try:
                response = requests.post(
                    url,
                    json={"offset": offset, "limit": 100},
                    verify=True
                )
                
                data = response.json()
                
                if data.get('status') == 'ok':
                    updated = data.get('updated', 0)
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Sincronizados {updated} productos (offset: {offset})"
                        )
                    )
                    
                    if updated == 0:
                        break
                    
                    offset = data.get('offset', offset + 100)
                else:
                    self.stdout.write(
                        self.style.ERROR(f"Error: {data.get('error')}")
                    )
                    break
                    
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"Error: {str(e)}")
                )
                break
        
        self.stdout.write(self.style.SUCCESS("Sincronización completada"))