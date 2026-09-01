import requests
from datetime import datetime
from django.contrib import admin, messages
from django.utils.html import format_html
from django.conf import settings
from django.urls import path
from django.http import HttpResponseRedirect
from django.shortcuts import render
from core.forms import SucursalForm
from core.models import FailedOrder, Sucursal
from core.sucursales import completar_desde_woocommerce
from core.woocommerce import wc_api


from django.core.management import call_command
from django.db import connection

@admin.register(FailedOrder)
class FailedOrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_id",
        # Durante la expansión se muestran las dos: si alguna vez difieren, la
        # pantalla lo delata sin necesidad de entrar a la base.
        "external_reference",
        "colored_status",
        "bims_invoice_number",
        "bims_sale_id",
        "message",
    )
    list_display_links = ("order_id", "colored_status")
    search_fields = (
        "order_id",
        "external_reference",
        "bims_sale_id",
        "bims_invoice_number",
    )
    ordering = ("status", "order_id")
    list_filter = ("status", "origin")
    actions = ["retry_selected_orders", "mark_as_failed"]

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        tables = connection.introspection.table_names()
        extra_context["contact_cache_table_exists"] = "core_contactcache" in tables
        return super().changelist_view(request, extra_context=extra_context)

    def colored_status(self, obj):
        # Por constante y no por número: con seis estados, un dict {1: ..., 2: ...}
        # deja los nuevos en negro y la pantalla deja de informar.
        colors = {
            FailedOrder.FAILED: "red",
            FailedOrder.COMPLETED: "#00B26B",
            FailedOrder.PENDING: "#F37043",
            FailedOrder.PROCESSING: "#6950A1",
            FailedOrder.PAUSED: "#F17DB1",
            FailedOrder.NOT_APPLICABLE: "gray",
        }
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

    def mark_as_failed(self, request, queryset):
        """Marks the selected orders as failed."""
        updated = queryset.update(status=FailedOrder.FAILED, message="Marcado como fallido manualmente.")
        self.message_user(
            request,
            f"{updated} orden(es) marcada(s) como fallida(s).",
            level=messages.SUCCESS,
        )

    mark_as_failed.short_description = "Marcar como fallidas"

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


@admin.register(Sucursal)
class SucursalAdmin(admin.ModelAdmin):
    """
    Alta y edición de sucursales sin redesplegar.

    El mapeo cajero POS → punto de venta de BIMS vivía en `core/constants.py`.
    Acá se ve y se edita: cargás el email del cajero y `save_model` resuelve su
    ID contra WooCommerce (o al revés, si cargás el ID).
    """

    list_display = (
        "nombre",
        "tipo",
        "email",
        "wp_user_id",
        "punto_de_venta",
        "updated_at",
    )
    form = SucursalForm
    list_display_links = ("nombre",)
    # Varios cajeros pueden compartir un punto de venta, así que filtrar por él
    # es la forma de ver todas las cajas de una misma sucursal.
    list_filter = ("tipo", "bims_posale_id")
    search_fields = ("nombre", "email", "wp_user_id")
    ordering = ("tipo", "nombre")
    fields = ("tipo", "nombre", "email", "wp_user_id", "bims_posale_id")

    def punto_de_venta(self, obj):
        """Un punto de venta vacío no es un dato faltante: significa no facturar."""
        if obj.bims_posale_id is None:
            return format_html(
                '<span style="color: #F37043; font-weight: 600;">no facturar</span>'
            )
        return format_html(
            '<span style="color: #00B26B; font-weight: 600;">{}</span>',
            obj.bims_posale_id,
        )

    punto_de_venta.short_description = "Punto de venta BIMS"
    punto_de_venta.admin_order_field = "bims_posale_id"

    def save_model(self, request, obj, form, change):
        """
        Completa email ↔ ID contra WooCommerce antes de guardar.

        `completar_desde_woocommerce` nunca lanza: si WooCommerce no responde,
        guarda lo cargado y devuelve un aviso. Guardar no puede depender de que
        WooCommerce esté arriba.
        """
        avisos = [completar_desde_woocommerce(obj), getattr(form, "aviso_bims", None)]
        super().save_model(request, obj, form, change)
        for aviso in avisos:
            if aviso:
                self.message_user(request, aviso, level=messages.WARNING)
