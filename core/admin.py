import requests
from datetime import datetime
from django.contrib import admin, messages
from django.utils.html import format_html
from django.conf import settings
from django.urls import path
from django.http import HttpResponseRedirect
from core.models import FailedOrder


@admin.register(FailedOrder)
class FailedOrderAdmin(admin.ModelAdmin):
    list_display = ("order_id", "colored_status", "message")
    list_display_links = ("order_id", "colored_status")
    search_fields = ("order_id",)
    ordering = ("status", "order_id")
    list_filter = ("status",)
    actions = ["retry_selected_orders"]

    def colored_status(self, obj):
        colors = {1: "red", 2: "green"}
        return format_html(
            '<span style="color: {};">{}</span>',
            colors.get(obj.status, "black"),
            obj.get_status_display(),
        )

    colored_status.admin_order_field = "status"
    colored_status.short_description = "Estado"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "retry_failed_orders/",
                self.admin_site.admin_view(self.retry_failed_orders_button),
                name="retry_failed_orders",
            ),
        ]
        return custom_urls + urls

    def retry_failed_orders_button(self, request):
        """Processes all failed orders."""
        try:
            failed_orders = FailedOrder.objects.filter(status=FailedOrder.FAILED)
            url = settings.BASE_URL + "/sales/"

            for order in failed_orders:
                try:
                    response = requests.post(
                        url, json={"arg": order.order_id}, verify=True
                    )

                    if (
                        response.status_code == 200
                        and response.json().get("status") == "ok"
                    ):
                        order.status = FailedOrder.COMPLETED
                        order.message = "Procesado con éxito."
                        order.save()

                except Exception as e:
                    current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
                    order.message = (
                        f"{current_time} | Error processing order {order.order_id}: {e}"
                    )
                    order.save()

            self.message_user(
                request,
                "Las órdenes fallidas han sido procesadas correctamente.",
                level=messages.SUCCESS,
            )
        except Exception as e:
            self.message_user(
                request,
                f"Ocurrió un error inesperado al procesar las órdenes fallidas: {str(e)}",
                level=messages.ERROR,
            )

        return HttpResponseRedirect("..")

    def retry_selected_orders(self, request, queryset):
        """Retries processing the selected orders."""
        try:
            url = settings.BASE_URL + "/sales/"

            for order in queryset:
                if order.status == FailedOrder.FAILED:
                    try:
                        response = requests.post(
                            url, json={"arg": order.order_id}, verify=True
                        )

                        if (
                            response.status_code == 200
                            and response.json().get("status") == "ok"
                        ):
                            order.status = FailedOrder.COMPLETED
                            order.message = "Procesado con éxito."
                            order.save()

                    except Exception as e:
                        current_time = datetime.now().strftime("%d/%m/%Y %H:%M")
                        order.message = f"{current_time} | Error processing order {order.order_id}: {e}"
                        order.save()

            self.message_user(
                request,
                "Las órdenes seleccionadas han sido procesadas correctamente.",
                level=messages.SUCCESS,
            )
        except Exception as e:
            self.message_user(
                request,
                f"Ocurrió un error inesperado al procesar las órdenes seleccionadas: {str(e)}",
                level=messages.ERROR,
            )

    retry_selected_orders.short_description = "Reintentar órdenes seleccionadas"
