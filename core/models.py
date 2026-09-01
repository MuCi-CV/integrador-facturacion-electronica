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
    # ⚠️ Los dos de arriba YA EXISTEN en producción con esos valores y hay 8702
    # filas que dependen de ellos. Los nuevos se agregan arriba; renumerar
    # reescribiría el estado fiscal de toda la historia.
    PENDING = 3
    PROCESSING = 4
    PAUSED = 5
    NOT_APPLICABLE = 6
    STATUS_CHOICES = (
        (FAILED, "Fallido"),
        (COMPLETED, "Completado"),
        (PENDING, "Pendiente"),
        (PROCESSING, "En proceso"),
        (PAUSED, "Pausada"),
        (NOT_APPLICABLE, "No aplica"),
    )

    ORIGIN_WOO = "woo"
    # Sin uso en el sub-proyecto A: lo estrena F, la entrada de donaciones desde
    # Krayin. El esquema lo admite desde ya para no migrar dos veces la tabla de
    # estado fiscal.
    ORIGIN_CRM = "crm"
    ORIGIN_CHOICES = (
        (ORIGIN_WOO, "WooCommerce"),
        (ORIGIN_CRM, "CRM Krayin"),
    )

    order_id = models.IntegerField(verbose_name="ID de la orden")
    # Fase de EXPANSIÓN: convive con `order_id`, que sigue siendo la columna
    # heredada y la fuente de verdad. Nullable a propósito — las 8702 filas
    # existentes se llenan por migración de datos, y hasta que eso corra tiene
    # que poder estar vacía. La CONTRACCIÓN (borrar `order_id`) es una tarea
    # aparte y posterior: no se usa `RenameField` porque en MariaDB el DDL hace
    # commit implícito, así que la atomicidad que Django le da a una migración
    # no cubre el esquema y un fallo a mitad de camino no vuelve solo.
    external_reference = models.CharField(
        verbose_name="Referencia externa",
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )
    status = models.PositiveSmallIntegerField(
        verbose_name="Estado", choices=STATUS_CHOICES, default=FAILED, db_index=True
    )
    message = models.TextField(verbose_name="Mensaje", blank=True, null=True)
    # Correlación orden → factura de BIMS. `null` significa "no sabemos": lo son las
    # órdenes anteriores a este campo y las que fallaron antes de facturar. Se guardan
    # como texto porque BIMS es laxo con los tipos (devolvió `payment_method_id: "43"`
    # como string) y no hacemos aritmética con estos valores.
    bims_sale_id = models.CharField(
        verbose_name="ID de venta en BIMS",
        max_length=32,
        blank=True,
        null=True,
        db_index=True,
    )
    bims_invoice_number = models.CharField(
        verbose_name="Nº de factura", max_length=32, blank=True, null=True
    )
    origin = models.CharField(
        verbose_name="Origen",
        max_length=8,
        choices=ORIGIN_CHOICES,
        default=ORIGIN_WOO,
        db_index=True,
    )
    bims_attempts = models.PositiveSmallIntegerField(
        verbose_name="Intentos contra BIMS", default=0
    )
    bims_next_attempt = models.DateTimeField(
        verbose_name="Próximo intento", null=True, blank=True, db_index=True
    )
    # La rama de anotar en WooCommerce no lleva backoff propio: es una llamada
    # barata e idempotente y le alcanza con reintentarse en cada pasada. Darle
    # cronograma a cada rama serían nueve columnas cuando entre el CRM.
    woo_meta_ok = models.BooleanField(
        verbose_name="Anotada en WooCommerce", default=False
    )
    # Sin esto, una fila que quedó en PROCESSING porque el worker murió a mitad
    # de camino se queda ahí para siempre. Es el bug clásico de toda cola.
    claimed_at = models.DateTimeField(
        verbose_name="Tomada por el worker", null=True, blank=True
    )
    created_at = models.DateTimeField(default=now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Orden fallida"
        verbose_name_plural = "Órdenes fallidas"
        # La unicidad es por (origen, referencia) y no por referencia sola: el
        # mismo número puede existir en WooCommerce y en el CRM sin ser la misma
        # transacción. En MariaDB un UNIQUE admite múltiples NULL, así que el
        # constraint no molesta mientras las filas viejas estén sin llenar.
        unique_together = ("origin", "external_reference")

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
