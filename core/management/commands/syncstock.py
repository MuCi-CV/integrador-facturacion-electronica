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