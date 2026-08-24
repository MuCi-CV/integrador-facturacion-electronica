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
        verbose_name="Estado", choices=STATUS_CHOICES, default=FAILED, db_index=True
    )
    message = models.TextField(verbose_name="Mensaje", blank=True, null=True)
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Orden fallida"
        verbose_name_plural = "Órdenes fallidas"

    def __str__(self):
        return f"Orden {self.order_id}"


class Sucursal(models.Model):
    """
    Mapeo cajero POS de WordPress → punto de venta de BIMS.

    Vivía hardcodeado en `core/constants.py`, así que agregar una sucursal
    exigía editar código y redesplegar. Se edita desde el admin.

    Hay tres tipos: los `CAJERO` apuntan a un usuario concreto de WordPress; los
    otros dos son **reglas por defecto** y no tienen usuario ni email —
    `POS_SIN_MAPEO` cubre a cualquier cajero no registrado y `WEB` a las órdenes
    que llegan sin cajero. Esos dos son fila única.

    `bims_posale_id` vacío significa **no facturar**: es como se representa la
    cuenta de administrador, y sirve para dar de baja cualquier cajero sin
    borrarlo.
    """

    CAJERO = "cajero"
    POS_SIN_MAPEO = "pos_sin_mapeo"
    WEB = "web"
    TIPO_CHOICES = (
        (CAJERO, "Cajero POS"),
        (POS_SIN_MAPEO, "Regla: cualquier otro cajero POS"),
        (WEB, "Regla: órdenes web"),
    )
    TIPOS_SIN_USUARIO = (POS_SIN_MAPEO, WEB)

    tipo = models.CharField(
        verbose_name="Tipo",
        max_length=20,
        choices=TIPO_CHOICES,
        default=CAJERO,
        db_index=True,
    )
    nombre = models.CharField(verbose_name="Nombre", max_length=100)
    email = models.EmailField(
        verbose_name="Email del cajero en WordPress",
        blank=True,
        help_text="Cargá el email o el ID; el integrador completa el otro contra WooCommerce.",
    )
    wp_user_id = models.PositiveIntegerField(
        verbose_name="ID del cajero en WordPress",
        unique=True,
        null=True,
        blank=True,
    )
    bims_posale_id = models.PositiveIntegerField(
        verbose_name="Punto de venta en BIMS",
        null=True,
        blank=True,
        help_text="Vacío = no facturar las órdenes de esta sucursal.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Sucursal"
        verbose_name_plural = "Sucursales"
        ordering = ("tipo", "nombre")

    def __str__(self):
        return self.nombre

    def clean(self):
        """Impide dos filas del mismo tipo único: la resolución sería ambigua."""
        from django.core.exceptions import ValidationError

        if self.tipo in self.TIPOS_SIN_USUARIO:
            hermanas = Sucursal.objects.filter(tipo=self.tipo).exclude(pk=self.pk)
            if hermanas.exists():
                raise ValidationError(
                    {"tipo": "Ya existe una fila de tipo '{}'. Editá la que hay.".format(
                        self.get_tipo_display()
                    )}
                )


class RucCache(models.Model):
    ruc = models.CharField(
        verbose_name="RUC", max_length=20, unique=True, db_index=True
    )  # con dígito verificador: "80012345-6"
    razon_social = models.CharField(verbose_name="Razón social", max_length=255)
    checked_at = models.DateTimeField(verbose_name="Última consulta exitosa a la API")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Caché de RUC"
        verbose_name_plural = "Cachés de RUC"

    def __str__(self):
        return f"{self.ruc} - {self.razon_social}"
