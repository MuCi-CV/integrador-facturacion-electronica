from django.contrib import admin
from django.utils.html import format_html
from core.models import FailedOrder


@admin.register(FailedOrder)
class FailedOrderAdmin(admin.ModelAdmin):
    list_display = ("order_id", "colored_status")
    list_display_links = ("order_id", "colored_status")
    search_fields = ("order_id",)
    ordering = ("status", "order_id")
    list_filter = ("status",)

    def colored_status(self, obj):
        colors = {1: "red", 2: "green"}
        return format_html(
            '<span style="color: {};">{}</span>',
            colors.get(obj.status, "black"),
            obj.get_status_display(),
        )

    colored_status.admin_order_field = "status"
    colored_status.short_description = "Estado"
