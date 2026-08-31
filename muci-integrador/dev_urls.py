"""
urlconf mínimo para el dev server local: solo el admin. NO se commitea.

El urlconf real (`muci-integrador/urls.py`) incluye `core.urls`, que importa
`core.views` → `core.services` → `core.bims`, y ese último instancia `BimsApi()`
en el import haciendo login contra BIMS. Sin credenciales reales eso impide que
el servidor arranque, y para probar la pantalla de Sucursales no hace falta.
"""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]
