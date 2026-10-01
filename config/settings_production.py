"""Strict deployment settings for staging and production."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F401,F403


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"Required environment variable is missing: {name}")
    return value


def csv_env(name):
    values = [item.strip() for item in required_env(name).split(",") if item.strip()]
    if not values:
        raise ImproperlyConfigured(f"Required environment variable is empty: {name}")
    return values


DEBUG = False

SECRET_KEY = required_env("DJANGO_SECRET_KEY")
if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("replace-with-"):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be a non-placeholder value of at least 50 characters."
    )

ALLOWED_HOSTS = csv_env("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = csv_env("DJANGO_CSRF_TRUSTED_ORIGINS")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": required_env("DATABASE_HOST"),
        "PORT": required_env("DATABASE_PORT"),
        "NAME": required_env("DATABASE_NAME"),
        "USER": required_env("DATABASE_USER"),
        "PASSWORD": required_env("DATABASE_PASSWORD"),
        "CONN_MAX_AGE": int(os.getenv("DATABASE_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
    }
}

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None

SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

MEDIA_ROOT = Path(required_env("MEDIA_ROOT"))
# FileField.url now resolves through the authenticated portal route.
MEDIA_URL = "/portal/files/"

ALLOW_ADMIN_TEST_ORDER_PURGE = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("DJANGO_LOG_LEVEL", "INFO"),
    },
}
