import requests
from datetime import datetime
from django.contrib import admin, messages
from django.utils.html import format_html
from django.conf import settings
from django.urls import path
from django.http import HttpResponseRedirect
from django.shortcuts import render
from core.models import FailedOrder
from core.woocommerce import wc_api


from django.core.management import call_command
from django.db import connection

@admin.register(FailedOrder)
class FailedOrderAdmin(admin.ModelAdmin):
    list_display = ("order_id", "colored_status", "message")
    list_display_links = ("order_id", "colored_status")
    search_fields = ("order_id",)
    ordering = ("status", "order_id")
    list_filter = ("status",)
    actions = ["retry_selected_orders"]

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        tables = connection.introspection.table_names()
        extra_context["contact_cache_table_exists"] = "core_contactcache" in tables
        return super().changelist_view(request, extra_context=extra_context)

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
            path(
                "retry_failed_orders/",
                self.admin_site.admin_view(self.retry_failed_orders_button),
                name="retry_failed_orders",
            ),
            path(
                "search_product/",
                self.admin_site.admin_view(self.search_product_view),
                name="search_product",
            ),
            path(
                "search_order/",
                self.admin_site.admin_view(self.search_order_view),
                name="search_order",
            ),
            path(
                "run_migrations/",
                self.admin_site.admin_view(self.run_migrations_button),
                name="run_migrations",
            ),
        ]
        return custom_urls + urls

    def run_migrations_button(self, request):
        """Runs the makemigrations and migrate commands manually from UI."""
        try:
            call_command("migrate")
            self.message_user(
                request,
                "Migración ejecutada con éxito. La tabla ContactCache se ha creado.",
                level=messages.SUCCESS,
            )
        except Exception as e:
            self.message_user(
                request,
                f"Error al migrar la base de datos: {e}",
                level=messages.ERROR,
            )
        return HttpResponseRedirect("..")

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

    def search_product_view(self, request):
        """View to search for products by ID."""
        product_data = None
        error = None

        if request.method == "POST":
            product_id = request.POST.get("product_id")
            if product_id:
                try:
                    product_data = wc_api.get_product(product_id)
                except Exception as e:
                    error = str(e)

        return render(
            request,
            "admin/search_product.html",
            {"product_data": product_data, "error": error},
        )

    def search_order_view(self, request):
        """View to search for orders by ID."""
        order_data = None
        error = None

        if request.method == "POST":
            order_id = request.POST.get("order_id")
            if order_id:
                try:
                    order_data = wc_api.get_order(order_id)
                except Exception as e:
                    error = str(e)

        return render(
            request,
            "admin/search_order.html",
            {"order_data": order_data, "error": error},
        )
