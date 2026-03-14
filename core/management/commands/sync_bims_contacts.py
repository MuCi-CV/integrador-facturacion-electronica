from django.core.management.base import BaseCommand
from core.models import ContactCache
from core.bims import bims
import time
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Sincroniza los contactos de BIMS a la base de datos local para búsqueda rápida"

    def handle(self, *args, **options):
        self.stdout.write("Iniciando sincronización de contactos...")
        limit = 500
        offset = 0
        total_synced = 0
        
        while True:
            self.stdout.write(f"Descargando contactos {offset} a {offset+limit}...")
            try:
                response = bims.get_contacts(limit=limit, offset=offset)
            except Exception as e:
                self.stderr.write(f"Error al descargar contactos: {e}")
                break

            data = response.get("data", [])
            
            if not data:
                break
                
            for item in data:
                contact = item.get("Contact", {})
                bims_id = contact.get("id")
                email = contact.get("emails", "").strip()
                document_id = contact.get("document_id", "")
                if document_id:
                    document_id = document_id.strip()
                
                if bims_id and email:
                    # Tomar el primer email si hay comas
                    if "," in email:
                        email = email.split(",")[0].strip()
                        
                    ContactCache.objects.update_or_create(
                        bims_id=bims_id,
                        defaults={
                            "email": email,
                            "document_id": document_id
                        }
                    )
                    total_synced += 1
            
            offset += limit
            time.sleep(1)  # Prevenir rate limiting si lo hubiera
        self.stdout.write(self.style.SUCCESS(f"Sincronización completa. Total guardados: {total_synced}"))

        self.stdout.write("Buscando órdenes pausadas para auto-reintento...")
        from core.models import FailedOrder
        import requests
        from django.conf import settings
        
        paused_orders = FailedOrder.objects.filter(status=FailedOrder.FAILED, message__startswith="Pausada: Esperando")
        
        if not paused_orders.exists():
            self.stdout.write("No hay órdenes pausadas actualmente.")
            return

        url = settings.BASE_URL + "/sales/"
        for order in paused_orders:
            self.stdout.write(f"Reintentando orden pausada {order.order_id}...")
            try:
                response = requests.post(url, json={"arg": order.order_id}, verify=True, timeout=15)
                if response.status_code == 200 and response.json().get("status") == "ok":
                    order.status = FailedOrder.COMPLETED
                    order.message = "Procesado con éxito tras sincronización asíncrona."
                    order.save()
                    self.stdout.write(self.style.SUCCESS(f"Orden {order.order_id} procesada exitosamente."))
                else:
                    self.stderr.write(f"Fallo al reintentar orden {order.order_id}: {response.text}")
            except Exception as e:
                self.stderr.write(f"Error HTTP al reintentar orden {order.order_id}: {e}")
