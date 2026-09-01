from datetime import datetime

from django.core.management.base import BaseCommand

from core.models import FailedOrder
from core.states import enqueue


class Command(BaseCommand):
    help = "Reencola las órdenes fallidas para que las procese el worker de la cola."

    def handle(self, *args, **options):
        """
        Devuelve las fallidas a la cola. No factura nada: eso lo hace el worker.

        Antes esto hacía un POST a `/sales/` por orden y sólo actuaba si la
        respuesta era 200. Con el ingreso devolviendo 202 esa condición no vuelve
        a cumplirse nunca, así que el comando habría seguido corriendo sin error
        y sin hacer absolutamente nada. Escribir en la BD elimina el intermediario
        HTTP y con él esa clase de falla.
        """
        reencoladas = 0
        for orden in FailedOrder.objects.filter(status=FailedOrder.FAILED):
            try:
                # Una fila creada entre el `migrate` y el `restart` del despliegue
                # tiene `external_reference` en NULL. `order_id` es el rescate:
                # sin él esa orden nunca se reintentaría.
                enqueue(orden.external_reference or orden.order_id, orden.origin)
                reencoladas += 1
            except ValueError as e:
                momento = datetime.now().strftime("%d/%m/%Y %H:%M")
                self.stderr.write(
                    f"{momento} | No se pudo reencolar la orden {orden.order_id}: {e}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Reencoladas: {reencoladas}. Las procesa el worker de la cola."
            )
        )
