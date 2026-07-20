from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.contact_lookup import lookup_contact


class ContactLookupView(APIView):
    """
    GET /contact-lookup/?ruc=...  ó  ?email=...
    Requiere header X-Muci-Token con el secreto compartido.
    Devuelve PII (nombre/correo/RUC) para autocompletar el POS.
    """

    def get(self, request):
        expected = getattr(settings, "POS_LOOKUP_TOKEN", None)
        provided = request.headers.get("X-Muci-Token")
        if not expected or provided != expected:
            return Response({"error": "unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)

        ruc = request.query_params.get("ruc") or None
        email = request.query_params.get("email") or None
        if not ruc and not email:
            return Response(
                {"error": "Se requiere 'ruc' o 'email'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(lookup_contact(ruc=ruc, email=email))
