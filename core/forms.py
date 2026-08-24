"""
Formularios del admin.

`SucursalForm` existe por una razón concreta: el punto de venta lo define BIMS,
y escribirlo a mano permitía cargar un ID inexistente cuyo error aparecía recién
al facturar una venta real. Acá se elige de la lista que devuelve BIMS.
"""

from django import forms

from core.models import Sucursal
from core.sucursales import opciones_de_punto_de_venta

SIN_FACTURAR = "no facturar"


class SucursalForm(forms.ModelForm):
    """
    Formulario de sucursal con el punto de venta como desplegable.

    Si BIMS responde, `bims_posale_id` se reemplaza por un desplegable con los
    puntos de venta reales; un ID fuera de esa lista queda rechazado por el
    propio campo. Si BIMS no responde, se deja el campo numérico original y
    `aviso_bims` explica por qué — el alta nunca se bloquea.
    """

    class Meta:
        model = Sucursal
        fields = ("tipo", "nombre", "email", "wp_user_id", "bims_posale_id")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opciones_bims, self.aviso_bims = opciones_de_punto_de_venta()

        if not self.opciones_bims:
            # Modo degradado: queda el campo numérico. El aviso va en el
            # help_text porque es donde el usuario lo va a leer.
            campo = self.fields["bims_posale_id"]
            campo.help_text = f"{self.aviso_bims} Vacío = {SIN_FACTURAR}."
            return

        self.fields["bims_posale_id"] = forms.TypedChoiceField(
            label="Punto de venta en BIMS",
            # La opción vacía no es "sin elegir": es la forma de decir que las
            # órdenes de esta sucursal no se facturan.
            choices=[("", f"— {SIN_FACTURAR} —")]
            + [(str(id_), f"{id_} — {nombre}") for id_, nombre in self.opciones_bims],
            coerce=int,
            empty_value=None,
            required=False,
            help_text=f"Lista traída de BIMS. Dejalo en «{SIN_FACTURAR}» para no facturar.",
        )
