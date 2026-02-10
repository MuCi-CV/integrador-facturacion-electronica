from django.contrib import admin
from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import user_passes_test
from django.views.decorators.http import require_http_methods
from core.bims import BimsApi
from django.conf import settings
import hashlib


@user_passes_test(lambda u: u.is_superuser)
@require_http_methods(["GET", "POST"])
def bims_diagnostic_view(request):
    """Vista de diagnóstico para testear el login a BIMS API."""
    
    context = {
        "title": "BIMS API - Panel de Diagnóstico",
        "bims_url": settings.BIMS_URL,
        "bims_user": settings.BIMS_USER,
        "bims_tenant": settings.BIMS_TENANT,
    }
    
    if request.method == "POST":
        # Testear conexión con las credenciales actuales
        test_api = BimsApi()
        
        # Intentar login
        session_id = test_api.login()
        
        context["test_executed"] = True
        context["login_successful"] = session_id is not None
        context["session_id"] = session_id
        context["error_message"] = test_api._login_error
        context["password_hash"] = hashlib.md5(settings.BIMS_PASSWORD.encode()).hexdigest()
        
        # Información adicional
        if session_id:
            context["status"] = "success"
            context["message"] = "✅ Login exitoso a BIMS API"
        else:
            context["status"] = "error"
            context["message"] = "❌ Error al conectar con BIMS API"
    
    return render(request, "admin/bims_diagnostic.html", context)
