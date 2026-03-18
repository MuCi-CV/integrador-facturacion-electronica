import os
import json
from pathlib import Path
from dotenv import dotenv_values
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
import logging

sentry_sdk.init(
    dsn="https://695919a3d075c89f4368be07b5e67f46@o4509430066315264.ingest.us.sentry.io/4509430068609024",
    integrations=[
        DjangoIntegration(),
        LoggingIntegration(
            level=logging.INFO, # Captura logs de nivel INFO o superior de Python (aparecen en journalctl)
            event_level=logging.ERROR # Solo envía a Sentry logs de nivel ERROR o superior
        ),
    ],
    # Set traces_sample_rate to 1.0 to capture 100%
    # of transactions for performance monitoring.
    traces_sample_rate=1.0,
    # Set profiles_sample_rate to 1.0 to profile 100%
    # of sampled transactions.
    # We recommend adjusting this value in production.
    profiles_sample_rate=1.0,
    release="integrador-facturacion-electronica@1.0.2",
    environment="production",
)

config = dotenv_values(".env")

BASE_DIR = Path(__file__).resolve().parent.parent


SECRET_KEY = config.get("SECRET_KEY")

DEBUG = True if config.get("DEBUG").lower() in ["true", 1] else False

ALLOWED_HOSTS = config.get("ALLOWED_HOSTS").split(",")


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

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "muci-integrador.urls"

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

WSGI_APPLICATION = "muci-integrador.wsgi.application"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": config["DB_NAME"],
        "USER": config["DB_USER"],
        "PASSWORD": config["DB_PASSWORD"],
        "HOST": config["DB_HOST"],
        "PORT": int(config["DB_PORT"]),
    }
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '%(asctime)s - %(levelname)s - %(module)s - %(message)s'
        },
        'bims_api_fmt': {
            'format': '%(asctime)s - %(levelname)s - %(message)s'
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'level': 'INFO',
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'bims_sync.log',
            'formatter': 'verbose',
        },
        'bims_api_file': {
            'level': 'DEBUG',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'bims_api.log',
            'maxBytes': 5 * 1024 * 1024,  # 5 MB
            'backupCount': 3,
            'formatter': 'bims_api_fmt',
        },
    },
    'loggers': {
        'core': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': True,
        },
        '': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
        },
        'bims_api': {
            'handlers': ['bims_api_file'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
}


LANGUAGE_CODE = "es-PY"

TIME_ZONE = "America/Asuncion"

USE_I18N = True

USE_L10N = True

USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CORS_ALLOW_ALL_ORIGINS = True

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": (
        "djangorestframework_camel_case.render.CamelCaseJSONRenderer",
        "djangorestframework_camel_case.render.CamelCaseBrowsableAPIRenderer",
    ),
    "DEFAULT_PARSER_CLASSES": (
        "djangorestframework_camel_case.parser.CamelCaseFormParser",
        "djangorestframework_camel_case.parser.CamelCaseMultiPartParser",
        "djangorestframework_camel_case.parser.CamelCaseJSONParser",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "rest_framework.filters.OrderingFilter",
        "rest_framework.filters.SearchFilter",
    ),
}

if DEBUG:
    # Para desarrollo: los emails se muestran en la consola
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
else:
    # Para producción: configura según tu proveedor de email
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = config.get("EMAIL_HOST", "smtp.gmail.com")
    EMAIL_PORT = int(config.get("EMAIL_PORT", 587))
    EMAIL_USE_TLS = True if config.get("EMAIL_USE_TLS", "true").lower() in ["true", "1"] else False
    EMAIL_HOST_USER = config.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = config.get("EMAIL_HOST_PASSWORD", "")
    DEFAULT_FROM_EMAIL = config.get("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER)

# Configuración del sitio para los emails de recuperación
SITE_ID = 1
if config.get("BASE_URL"):
    # Configurar el dominio para los enlaces de recuperación
    from django.urls import get_script_prefix
    BASE_URL = config.get("BASE_URL")
    if BASE_URL:
        # Extraer el dominio de BASE_URL
        from urllib.parse import urlparse
        parsed_url = urlparse(BASE_URL)
        SITE_DOMAIN = parsed_url.netloc
        SITE_NAME = "MUCi Integrador"

WOOCOMMERCE_URL = config["WOOCOMMERCE_URL"]
WOOCOMMERCE_KEY = config["WOOCOMMERCE_KEY"]
WOOCOMMERCE_SECRET = config["WOOCOMMERCE_SECRET"]
BIMS_URL = config.get("BIMS_URL")
BIMS_USER = config.get("BIMS_USER")
BIMS_PASSWORD = config.get("BIMS_PASSWORD")
BIMS_TENANT = config.get("BIMS_TENANT")
BASE_URL = config.get("BASE_URL")