import logging

from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.states import enqueue
from core.woocommerce import wc_api

logger = logging.getLogger(__name__)


class SalesView(APIView):
    """
    Persiste la transacción y sale. El trabajo lo hace el worker de la cola.

    Devolver 202 y no el resultado de la facturación es deliberado: WooCommerce
    deshabilita un webhook a las 5 respuestas no-2xx seguidas, y la vista vieja
    contestaba 503 ante cualquier excepción. Una caída de BIMS de cinco órdenes
    apagaba `Venta Entrada` y la facturación se cortaba en silencio — así murió
    el webhook `Refund order`, que quedó con `failure_count 6`.

    El único no-2xx que queda es el 400 por request malformado, que depende de
    quien llama y no de un tercero.
    """

    def post(self, request):
        order_id = request.data.get("arg")

        if not order_id:
            logger.error("No se recibió 'order_id' en la solicitud.")
            return Response(
                data={"status": "fail", "error": "No se recibió 'order_id'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            enqueue(order_id)
        except ValueError as e:
            # Referencia malformada: culpa del request, no de un tercero. Un 500
            # le contaría a Woo como falla igual que el 503 que acabamos de sacar.
            logger.error(f"Referencia inválida en el ingreso: {e}")
            return Response(
                data={"status": "fail", "error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            data={"status": "encolada"}, status=status.HTTP_202_ACCEPTED
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
