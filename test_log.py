import os
import django
import logging

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "muci-integrador.settings")
django.setup()

logger = logging.getLogger("core.views")
logger.info("Test log from Django configuration")
print("Done writing to logger")
