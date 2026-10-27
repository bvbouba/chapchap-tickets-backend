"""Django settings for focused PrintMe tests.

Importing ``saleor`` normally executes ``saleor/__init__.py``, which pulls in
the rest of the Saleor stack and the shared database configuration. This
module registers the package path first so these model tests can use SQLite.
The Saleor suite does not use this module.
"""

import sys
import types
from pathlib import Path

_package = sys.modules.get("saleor")
if _package is None:
    _package = types.ModuleType("saleor")
    _package.__path__ = [str(Path(__file__).resolve().parent / "saleor")]
    _package.__package__ = "saleor"
    sys.modules["saleor"] = _package

SECRET_KEY = "printme-test-only"
DEBUG = False
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "saleor.printme",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

ROOT_URLCONF = "saleor.printme.tests.urls"
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Isolated tests only. Production keeps Django's default hasher via saleor.settings.
PASSWORD_HASHERS = [
    "saleor.printme.tests.hashers.FastPBKDF2PasswordHasher",
]

# Same defaults as saleor.settings. Service tests stay on this module so
# they do not import the Saleor settings stack or its database URL.
PRINTME_MAX_BYTES = 20 * 1024 * 1024
PRINTME_CODE_TTL_HOURS = 24
PRINTME_MAX_VERIFY_ATTEMPTS = 5
PRINTME_CODE_KEY = ""
PRINTME_SES_FROM_ADDRESS = "noreply@bavasim.com"
PRINTME_EMAIL_BACKEND = "django"
PRINTME_INBOUND_ADDRESS = ""
PRINTME_S3_BUCKET = "printme-test-bucket"
PRINTME_S3_PREFIX = "raw/"
PRINTME_AWS_REGION = ""
PRINTME_AWS_ACCESS_KEY_ID = ""
PRINTME_AWS_SECRET_ACCESS_KEY = ""
