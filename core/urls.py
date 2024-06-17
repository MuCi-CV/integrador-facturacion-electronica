from django.urls import path, re_path, include

from core.views import SalesView, RefundView


urlpatterns = [
    path("sales/", SalesView.as_view()),
    path("refunds/", RefundView.as_view()),
]
