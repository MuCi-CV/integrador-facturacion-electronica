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
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Orden fallida"
        verbose_name_plural = "Órdenes fallidas"

    def __str__(self):
        return f"Orden {self.order_id}"
