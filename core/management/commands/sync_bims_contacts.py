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
                email = contact.get("emails") or ""
                email = email.strip()
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
        
        # Antes esto filtraba por (FAILED, message startswith "Pausada:
        # Esperando"): el estado de pausa viajaba en el TEXTO del mensaje, así
        # que reformular ese texto rompía el comando en silencio. Ahora es un
        # estado de verdad. La migración 0012 movió las filas históricas.
        paused_orders = FailedOrder.objects.filter(status=FailedOrder.PAUSED)
        
        if not paused_orders.exists():
            self.stdout.write("No hay órdenes pausadas actualmente.")
            return

        url = settings.BASE_URL + "/sales/"
        for order in paused_orders:
            self.stdout.write(f"Reintentando orden pausada {order.order_id}...")
            try:
                response = requests.post(url, json={"arg": order.order_id}, verify=True, timeout=30)
                resp_json = response.json()
                resp_status = resp_json.get("status")
                resp_message = resp_json.get("message", "")
                
                if response.status_code == 200 and resp_status == "ok":
                    order.status = FailedOrder.COMPLETED
                    order.message = f"Procesado con éxito tras sincronización asíncrona. Detalle: {resp_message}"
                    order.save()
                    self.stdout.write(self.style.SUCCESS(f"Orden {order.order_id} procesada exitosamente en BIMS."))
                elif resp_status == "paused":
                    self.stderr.write(f"Orden {order.order_id} se volvió a pausar (email aún no encontrado en caché). Se reintentará en la próxima sincronización.")
                else:
                    error_detail = resp_json.get("error", response.text)
                    self.stderr.write(f"Fallo al reintentar orden {order.order_id}: {error_detail}")
            except Exception as e:
                self.stderr.write(f"Error HTTP al reintentar orden {order.order_id}: {e}")
