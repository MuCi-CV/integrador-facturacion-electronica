import logging

import sentry_sdk
from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.services import process_order
from core.woocommerce import wc_api

logger = logging.getLogger(__name__)


class SalesView(APIView):
    def post(self, request):
        order_id = request.data.get("arg")

        if not order_id:
            logger.error("No se recibió 'order_id' en la solicitud.")
            return Response(
                data={"status": "fail", "error": "No se recibió 'order_id'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with sentry_sdk.configure_scope() as scope:
            scope.set_tag("order_id", order_id)

            try:
                result = process_order(order_id)
                return Response(data=result)
            except ValueError as e:
                # Error de negocio (sin productos válidos, rechazado por BIMS)
                return Response(
                    data={"status": "fail", "error": str(e)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            except Exception as e:
                logger.error(f"Error procesando orden {order_id}: {e}", exc_info=True)
                return Response(
                    data={"status": "fail", "error": str(e)},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )


class RefundView(APIView):
    def post(self, request):
        order_id = request.data.get("arg")
        order = wc_api.get_order(order_id)

        products = [
            {
                "product_id": item.get("product_id"),
                "variation_id": item.get("variation_id"),
                "quantity": item.get("quantity"),
            }
            for item in order.get("line_items", [])
        ]

        wc_api.refund_order(id=order_id, data={"api_refund": False, "api_restock": True, "line_items": products})
        return Response(data={"status": "ok"})


class ForgotPasswordView(APIView):
    def post(self, request):
        try:
            email = request.data.get("email", "").strip()
            if not email:
                return Response(
                    {"error": "Por favor proporciona un email válido"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            logger.info(f"Solicitud de recuperación de contraseña para: {email}")
            return Response({
                "message": f"Solicitud recibida para {email}. El administrador contactará contigo pronto."
            })
        except Exception as e:
            logger.error(f"Error en ForgotPasswordView: {e}")
            return Response(
                {"error": "Error interno del servidor."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class SimpleForgotPasswordView(TemplateView):
    template_name = "forgot_password.html"
