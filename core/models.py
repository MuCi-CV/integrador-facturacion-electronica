from django.db import models
from django.utils.timezone import now


class FailedOrder(models.Model):

    FAILED = 1
    COMPLETED = 2
    STATUS_CHOICES = (
        (FAILED, "Fallido"),
        (COMPLETED, "Completado"),
    )

    order_id = models.IntegerField(verbose_name="ID de la orden")
    status = models.PositiveSmallIntegerField(
        verbose_name="Estado", choices=STATUS_CHOICES, default=FAILED
    )
    message = models.TextField(verbose_name="Mensaje", blank=True, null=True)
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Orden fallida"
        verbose_name_plural = "Órdenes fallidas"

    def __str__(self):
        return f"Orden {self.order_id}"

class ProductMapping(models.Model):
    """
    Mapeo entre productos de BIMS y WooCommerce
    Evita consultas lentas a la API de WooCommerce
    """
    bims_id = models.IntegerField(unique=True, db_index=True)
    wc_product_id = models.IntegerField()
    wc_parent_id = models.IntegerField(null=True, blank=True)
    product_type = models.CharField(max_length=20)  # simple, variable, variation
    sku = models.CharField(max_length=255, blank=True, null=True)
    name = models.CharField(max_length=255, blank=True, null=True)
    last_synced = models.DateTimeField(default=now)
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Mapeo de Producto"
        verbose_name_plural = "Mapeos de Productos"
        indexes = [
            models.Index(fields=['bims_id']),
            models.Index(fields=['wc_product_id']),
        ]
    
    def __str__(self):
        return f"BIMS {self.bims_id} → WC {self.wc_product_id} ({self.product_type})"
