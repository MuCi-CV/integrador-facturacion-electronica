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

        self.stdout.write("Buscando órdenes pausadas para reencolar...")
        from core.models import FailedOrder
        from core.states import enqueue

        # Antes esto filtraba por (FAILED, message startswith "Pausada:
        # Esperando"): el estado de pausa viajaba en el TEXTO del mensaje, así
        # que reformular ese texto rompía el comando en silencio. Ahora es un
        # estado de verdad. La migración 0012 movió las filas históricas.
        paused_orders = FailedOrder.objects.filter(status=FailedOrder.PAUSED)

        if not paused_orders.exists():
            self.stdout.write("No hay órdenes pausadas actualmente.")
            return

        # Escribe en la cola en vez de hacer un POST a `/sales/`. El código viejo
        # exigía `status_code == 200` y además ramificaba sobre `status ==
        # "paused"`, una respuesta que la vista dejó de dar el 2026-03-17: con el
        # 202 del ingreso asíncrono habría quedado sin hacer nada y sin avisar.
        reencoladas = 0
        for order in paused_orders:
            try:
                enqueue(order.external_reference or order.order_id, order.origin)
                reencoladas += 1
            except ValueError as e:
                self.stderr.write(
                    f"No se pudo reencolar la orden pausada {order.order_id}: {e}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Reencoladas {reencoladas} orden(es) pausada(s). "
                "Las procesa el worker de la cola."
            )
        )
