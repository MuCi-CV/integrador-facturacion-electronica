"""
Settings SOLO para probar la pantalla de Sucursales en local. NO se commitea.

Por qué es standalone y no `from .settings import *`:

1. `settings.py` no se puede importar sin un `.env` — usa `config["DB_NAME"]` y
   compañía con corchetes, así que sin archivo explota en el import.
2. Apunta a MySQL. Acá queremos SQLite, aislado y desechable.
3. El urlconf real incluye `core.urls` → `core.views` → `core.services` →
   `core.bims`, y ese módulo instancia `BimsApi()` **en el import**, que hace
   login contra BIMS. Sin credenciales reales el servidor no arranca. Por eso
   `dev_urls.py` monta solo el admin.

La base es un archivo, así que las sucursales que cargues sobreviven al reinicio.

Uso:
    .venv/bin/python manage.py migrate       --settings=muci-integrador.dev_settings
    .venv/bin/python manage.py runserver     --settings=muci-integrador.dev_settings
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "dev-only-no-usar-en-produccion"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "muci-integrador.dev_urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "dev-sucursales.sqlite3",
    }
}

STATIC_URL = "/static/"
LANGUAGE_CODE = "es-PY"
TIME_ZONE = "America/Asuncion"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Consola en INFO para ver los avisos de `core.sucursales`: cuándo cae a las
# constantes y cuándo un cajero no está registrado.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "core": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# ⚠️ Las llaves de WooCommerce en `.env.local` son los placeholders del ejemplo
# (`ck_xxx...`), así que no sirven. Con estos valores por defecto la resolución
# email → ID falla y se ve el aviso de degradación, que es una de las cosas a
# probar.
WOOCOMMERCE_URL = "https://ejemplo.invalid"
WOOCOMMERCE_KEY = "ck_dev"
WOOCOMMERCE_SECRET = "cs_dev"
WOOCOMMERCE_VERIFY_SSL = False

# Para probar la resolución REAL contra la tienda, traé solo las tres variables
# de WooCommerce del servidor a un archivo local (lo tapa la regla `.env.*` del
# .gitignore, así que no puede terminar en un commit):
#
#   ssh root@159.89.228.18 'grep ^WOOCOMMERCE_ /var/www/integrador/.env' > .env.woo
#
# Si el archivo existe, sus valores ganan. Borralo y volvés al modo degradado.
_ENV_WOO = BASE_DIR / ".env.woo"
if _ENV_WOO.exists():
    from dotenv import dotenv_values

    _woo = dotenv_values(str(_ENV_WOO))
    WOOCOMMERCE_URL = _woo.get("WOOCOMMERCE_URL", WOOCOMMERCE_URL)
    WOOCOMMERCE_KEY = _woo.get("WOOCOMMERCE_KEY", WOOCOMMERCE_KEY)
    WOOCOMMERCE_SECRET = _woo.get("WOOCOMMERCE_SECRET", WOOCOMMERCE_SECRET)
    WOOCOMMERCE_VERIFY_SSL = _woo.get("WOOCOMMERCE_VERIFY_SSL", "true").lower() not in (
        "false",
        "0",
    )

# Presentes solo para que importe `core.admin`; no se usan en esta pantalla.
BASE_URL = "http://localhost:8000"
