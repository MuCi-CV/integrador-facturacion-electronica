from django.db import models
from django.utils.timezone import now

class ContactCache(models.Model):
    bims_id = models.IntegerField(verbose_name="ID en BIMS", unique=True)
    email = models.EmailField(verbose_name="Email", db_index=True)
    document_id = models.CharField(verbose_name="Documento", max_length=50, blank=True, null=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Caché de Contacto"
        verbose_name_plural = "Cachés de Contactos"

    def __str__(self):
        return f"{self.email} - {self.bims_id}"


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
