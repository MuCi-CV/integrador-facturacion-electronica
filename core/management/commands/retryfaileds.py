import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from core.models import FailedOrder
from datetime import datetime


# Estados terminales del integrador: la orden ya no va a llegar a BIMS por decisión
# de negocio (monto 0, descuento total, cortesía, todos los productos en 0).
# Reintentarlas no cambia nada, así que se cierran para sacarlas de la cola.
DISCARDED_STATUSES = ("Productos en 0", "Monto 0", "Descuento 100%", "No procesado")


class Command(BaseCommand):
    help = "Retries processing failed orders and marks them as completed if successful."

    def handle(self, *args, **options):
        try:
            failed_orders = FailedOrder.objects.filter(status=FailedOrder.FAILED)
            url = settings.BASE_URL + "/sales/"

            for order in failed_orders:
                try:
                    response = requests.post(
                        url, json={"arg": order.order_id}, verify=True
                    )

                    if response.status_code == 200:
                        result_status = response.json().get("status")

                        if result_status == "ok":
                            order.status = FailedOrder.COMPLETED
                            order.message = "Procesado con éxito."
                            order.save()
                        elif result_status in DISCARDED_STATUSES:
                            order.status = FailedOrder.COMPLETED
                            order.message = f"Cerrada sin enviar a BIMS: {result_status}."
                            order.save()

                except Exception as e:
                    current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
                    self.stdout.write(
                        self.style.ERROR(
                            f"{current_time} | Error processing order {order.order_id}: {e}"
                        )
                    )
                    continue

        except Exception as e:
            current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
            self.stdout.write(
                self.style.ERROR(
                    f"{current_time} | An error occurred while processing failed orders."
                )
            )
            self.stdout.write(str(e))
