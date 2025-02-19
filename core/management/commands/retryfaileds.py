from django.core.management.base import BaseCommand
from django.test import RequestFactory
from core.models import FailedOrder
from core.views import SalesView


class Command(BaseCommand):
    help = "Retries processing failed orders and marks them as completed if successful."

    def handle(self, *args, **options):
        try:
            failed_orders = FailedOrder.objects.filter(status=FailedOrder.FAILED)
            factory = RequestFactory()

            for order in failed_orders:
                request = factory.post("/", {"arg": order.order_id})
                response = SalesView.as_view()(request).data

                if response.get("status") == "ok":
                    order.status = FailedOrder.COMPLETED
                    order.save()

        except Exception as e:
            self.stdout.write(
                self.style.ERROR("An error occurred while processing failed orders.")
            )
            self.stdout.write(str(e))
