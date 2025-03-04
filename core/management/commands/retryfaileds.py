import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from core.models import FailedOrder


class Command(BaseCommand):
    help = "Retries processing failed orders and marks them as completed if successful."

    def handle(self, *args, **options):
        try:
            failed_orders = FailedOrder.objects.filter(status=FailedOrder.FAILED)
            url = settings.BASE_URL + "/sales/"

            for order in failed_orders:
                response = requests.post(url, json={"arg": order.order_id}, verify=True)

                if (
                    response.status_code == 200
                    and response.json().get("status") == "ok"
                ):
                    order.status = FailedOrder.COMPLETED
                    order.message = "Procesado con éxito."
                    order.save()

        except Exception as e:
            self.stdout.write(
                self.style.ERROR("An error occurred while processing failed orders.")
            )
            self.stdout.write(str(e))
