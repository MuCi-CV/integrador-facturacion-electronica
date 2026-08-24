"""
Settings mínimos para correr tests unitarios sin .env ni base de datos externa.
Usar con: python manage.py test core/ --settings=muci-integrador.test_settings
"""
SECRET_KEY = "test-secret-key-only-for-tests"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "core",
]

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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
