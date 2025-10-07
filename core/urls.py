from django.urls import path, re_path, include

from core.views import SalesView, RefundView, SimpleForgotPasswordView, ForgotPasswordView

urlpatterns = [
    path("sales/", SalesView.as_view()),
    path("refunds/", RefundView.as_view()),
    path("forgot-password/", SimpleForgotPasswordView.as_view(), name="forgot-password"),
    path("api/forgot-password/", ForgotPasswordView.as_view(), name="api-forgot-password"),
]
