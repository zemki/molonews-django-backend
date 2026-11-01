"""Settings für die lokale Entwicklung."""

from .base import *  # noqa: F401,F403
from .base import env, env_list

DEBUG = True
SECRET_KEY = SECRET_KEY or "dev-only-insecure-key-do-not-use-in-production"  # noqa: F405
JWT_SECRET_KEY = JWT_SECRET_KEY or SECRET_KEY  # noqa: F405
SIMPLE_JWT["SIGNING_KEY"] = JWT_SECRET_KEY  # noqa: F405

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
CORS_ALLOW_ALL_ORIGINS = True

SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
