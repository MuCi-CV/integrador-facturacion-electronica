"""
Settings mínimos para correr tests unitarios sin .env ni base de datos externa.
Usar con: python manage.py test core/ --settings=muci-integrador.test_settings
"""
SECRET_KEY = "test-secret-key-only-for-tests"
DEBUG = True
ALLOWED_HOSTS = ["*"]

# Se cargan las mismas apps que producción. Antes eran 4 y la suite verde no
# probaba drf_yasg, corsheaders ni el admin, que es justo donde vive el riesgo
# de un upgrade de Django.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "drf_yasg",
    "corsheaders",
    "rest_framework",
    "core",
]

# El admin no funciona sin middleware de sesión, autenticación y mensajes.
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

# Ni sin plantillas con estos context processors.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ROOT_URLCONF = "muci-integrador.urls"
STATIC_URL = "static/"
STATIC_ROOT = "/tmp/static-tests"
# Explícito a propósito: Django 5.0 cambió el default a True y producción ya lo
# fija en True. Que los tests corran con otro valor es justo la clase de brecha
# que este upgrade viene a cerrar.
USE_TZ = True
TIME_ZONE = "America/Asuncion"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

LOGGING = {"version": 1, "disable_existing_loggers": True}

WOOCOMMERCE_URL = "http://test.local"
WOOCOMMERCE_KEY = "test_key"
WOOCOMMERCE_SECRET = "test_secret"
WOOCOMMERCE_VERIFY_SSL = False
BIMS_URL = "http://bims.test.local"
BIMS_FALLBACK_URL = None
BIMS_API_KEY = ""  # vacío => modo sesión (?sid=), que es el default de los tests
BIMS_USER = "test_user"
BIMS_PASSWORD = "test_password"
BIMS_TENANT = "test_tenant"
RUC_URL = None
RUC_API_URL = "http://turuc.test.local"
BASE_URL = "http://localhost"
POS_LOOKUP_TOKEN = "test-token"
QUEUE_REAPER_MINUTES = 10

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
