
from django.urls import path, re_path, include

from core.views import SalesView


urlpatterns = [
    path("sales/", SalesView.as_view())
   
]
