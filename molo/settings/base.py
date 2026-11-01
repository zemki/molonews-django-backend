"""
Basis-Settings für molo.

Alle instanzspezifischen Werte (Secrets, Hosts, Datenbank, Mail, KI) kommen
aus Umgebungsvariablen. Für die lokale Entwicklung kann eine Datei ``.env``
im Projekt-Root angelegt werden (Vorlage: ``.env.example``); sie wird beim
Start automatisch geladen, bereits gesetzte Variablen haben Vorrang.
"""

import os
from pathlib import Path

from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent.parent


# --------------------------------------------------------------------------
# .env laden (ohne externe Abhängigkeit)
# --------------------------------------------------------------------------
def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _sep, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(BASE_DIR / ".env")


def env(name, default=""):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# --------------------------------------------------------------------------
# Kern
# --------------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Öffentliche Basis-URL dieser Instanz (mit abschließendem Slash)
SERVER_URL = env("SERVER_URL", "http://localhost:8000/")

# Öffentliche Angaben der Instanz (Texte, Weiterleitungen, Geocoding)
WEBSITE_URL = env("WEBSITE_URL", SERVER_URL)
CONTACT_EMAIL = env("CONTACT_EMAIL")
APP_STORE_URL = env("APP_STORE_URL")
PLAY_STORE_URL = env("PLAY_STORE_URL")
# Nominatim verlangt einen aussagekräftigen User-Agent mit Kontakt
GEOCODER_USER_AGENT = env("GEOCODER_USER_AGENT", "molo-backend")

APPEND_SLASH = False
ROOT_URLCONF = "molo.urls"
WSGI_APPLICATION = "molo.wsgi.application"
AUTH_USER_MODEL = "content.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "content",
    "newadmin",
    "api",
    "rest_framework",
    "corsheaders",
    "django_filters",
    "drf_yasg",
    "molo.apps.JetConfig",
    "loginas",
    "versatileimagefield",
    "django_ckeditor_5",
    "recurrence",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "content.middleware_ios_upload.iOSUploadFixMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "content.middleware_flexible_slash.FlexibleSlashMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.i18n",
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

AUTHENTICATION_BACKENDS = [
    "content.authentication_backends.EmailOrUsernameBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --------------------------------------------------------------------------
# CORS / Cookies
# --------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", False)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = [
    "origin",
    "content-type",
    "x-auth-token",
    "x-requested-with",
    "authorization",
    "x-csrftoken",
    "x-device-id",
]

# Optional, z. B. ".example.org", wenn Admin und App auf Subdomains laufen
SESSION_COOKIE_DOMAIN = env("DJANGO_COOKIE_DOMAIN") or None
CSRF_COOKIE_DOMAIN = SESSION_COOKIE_DOMAIN
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"


# --------------------------------------------------------------------------
# Datenbank (PostgreSQL empfohlen; die KI-Suche nutzt Regex-Lookups)
# --------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": env("DB_ENGINE", "django.db.backends.postgresql"),
        "NAME": env("DB_NAME", "molo"),
        "USER": env("DB_USER", "molo"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env("DB_PORT", "5432"),
    }
}
if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    # Pfad der SQLite-Datei: SQLITE_PATH, sonst DB_NAME, wenn es ein Dateipfad ist
    # (das "molo" aus der PostgreSQL-Vorlage in .env.example ist keiner)
    _db_name = env("DB_NAME")
    _sqlite_path = env("SQLITE_PATH") or (
        _db_name if _db_name and (_db_name.endswith((".sqlite3", ".db")) or "/" in _db_name)
        else str(BASE_DIR / "db.sqlite3")
    )
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _sqlite_path,
    }


# --------------------------------------------------------------------------
# Sprache / Zeit
# --------------------------------------------------------------------------
LANGUAGE_CODE = "de"
LANGUAGES = [("de", _("German"))]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = env("DJANGO_TIME_ZONE", "Europe/Berlin")
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------
# Dateien und Verzeichnisse
# --------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = env("STATIC_ROOT", str(BASE_DIR / "www" / "static"))
MEDIA_URL = "/media/"
MEDIA_ROOT = env("MEDIA_ROOT", str(BASE_DIR / "www" / "media"))

# Laufzeitdaten (nicht im Repository)
LOG_DIR = Path(env("LOG_DIR", str(BASE_DIR / "logs")))
IMPORT_DATA_DIR = Path(env("IMPORT_DATA_DIR", str(BASE_DIR / "import_data")))
IMPORT_SCRIPTS_DIR = Path(env("IMPORT_SCRIPTS_DIR", str(BASE_DIR / "import_scripts")))

# Quellen-Links auf interne Adressen (localhost, LAN, Cloud-Metadaten) erlauben.
# Nur für lokale Entwicklung; in Produktion aus lassen (SSRF-Schutz).
ALLOW_PRIVATE_IMPORT_URLS = env_bool("ALLOW_PRIVATE_IMPORT_URLS", False)
BACKUP_DIR = Path(env("BACKUP_DIR", str(BASE_DIR / "backups")))

DATA_UPLOAD_MAX_NUMBER_FIELDS = 10240
DATA_UPLOAD_MAX_MEMORY_SIZE = 15 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 15 * 1024 * 1024


# --------------------------------------------------------------------------
# REST API / Authentifizierung
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.NamespaceVersioning",
    # API-Tokens prüft die Anwendung selbst (api.views.util.decode_access_token);
    # DRF nutzt nur die Sitzung der Weboberfläche.
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    # Ratenbegrenzung pro Client-IP für Login, Registrierung, Mails und KI.
    # Zählt pro Cache; für mehrere gunicorn-Worker einen gemeinsamen Cache
    # (z. B. Redis) konfigurieren, sonst gilt die Rate je Worker.
    "DEFAULT_THROTTLE_RATES": {
        "auth": env("THROTTLE_RATE_AUTH", "20/min"),
        "mail": env("THROTTLE_RATE_MAIL", "10/hour"),
        "ai": env("THROTTLE_RATE_AI", "30/min"),
    },
    # Anzahl vertrauenswürdiger Proxys vor Django (nginx = 1). Bestimmt, welche
    # Adresse aus X-Forwarded-For als Client-IP gilt; 0 = REMOTE_ADDR.
    "NUM_PROXIES": env_int("TRUSTED_PROXY_COUNT", 1),
}

# Gültigkeit von Passwort-Reset-Links
PASSWORD_RESET_TIMEOUT = env_int("PASSWORD_RESET_TIMEOUT_HOURS", 24) * 3600

JWT_SECRET_KEY = env("JWT_SECRET_KEY") or SECRET_KEY
JWT_ALGORITHM = "HS256"

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": __import__("datetime").timedelta(days=env_int("JWT_ACCESS_DAYS", 180)),
    "REFRESH_TOKEN_LIFETIME": __import__("datetime").timedelta(days=env_int("JWT_REFRESH_DAYS", 240)),
    # Keine Rotation/Blacklist: Widerruf über User.tokens_valid_after
    # (jede Passwortänderung macht ältere Access- und Refresh-Tokens ungültig)
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": False,
    "TOKEN_REFRESH_SERIALIZER": "api.tokens.CheckedTokenRefreshSerializer",
    "ALGORITHM": JWT_ALGORITHM,
    "SIGNING_KEY": JWT_SECRET_KEY,
    "VERIFYING_KEY": None,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "TOKEN_TYPE_CLAIM": "token_type",
}

# Token für den Datei-/Bild-Upload externer Importer
UPLOAD_AUTH_TOKEN = env("UPLOAD_AUTH_TOKEN")

# API-Key für externe Tagging-Worker (/api/v4/ai-tagging/...)
AI_TAGGING_API_KEY = env("AI_TAGGING_API_KEY")

LOGINAS_REDIRECT_URL = "/"


# --------------------------------------------------------------------------
# E-Mail
# --------------------------------------------------------------------------
# Djangos Mail-System (send_mail, EmailMessage); in dev.py zeigt EMAIL_BACKEND auf die Konsole
EMAIL_HOST = env("EMAIL_HOST")
EMAIL_PORT = env_int("EMAIL_PORT", 587)
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_HOST_USER = env("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL") or "webmaster@localhost"
SERVER_EMAIL = DEFAULT_FROM_EMAIL

MAIL_SENDING = {
    "EMAIL_HOST": env("EMAIL_HOST"),
    "EMAIL_PORT": env_int("EMAIL_PORT", 587),
    "EMAIL_USE_TLS": env_bool("EMAIL_USE_TLS", True),
    "EMAIL_HOST_USER": env("EMAIL_HOST_USER"),
    "EMAIL_HOST_PASSWORD": env("EMAIL_HOST_PASSWORD"),
    "DEFAULT_FROM_EMAIL": env("DEFAULT_FROM_EMAIL"),
    # Deep-Link der App für den Passwort-Reset
    "PASSWORD_RESET_URL": env("PASSWORD_RESET_URL", "molo://password-reset/"),
    "REDIRECT_AFTER_PASSWORD_RESET_REQUEST_PATH": "/api/v4/users/redirect-user-into-app",
    "ACTIVATION_URL": env("ACTIVATION_URL", SERVER_URL.rstrip("/") + "/api/v4/users/activate/"),
    # Empfänger für Fehler- und Monitoring-Meldungen (leer = keine Mails)
    "ERROR_NOTIFICATION_EMAIL": env("ERROR_NOTIFICATION_EMAIL"),
}

# Empfänger für Feedback-/Kontaktformulare der App
FEEDBACK_MAIL_TARGET = env("FEEDBACK_MAIL_TARGET") or None

# Gebiete für Geräte ohne eigene Gebietswahl (kommagetrennte IDs, leer = alle)
DEFAULT_AREA_IDS = [int(x) for x in env_list("DEFAULT_AREA_IDS") if x.isdigit()]

# RSS-Quellen, die nur einmal täglich abgerufen werden (kommagetrennte Source-IDs)
IMPORT_DAILY_SOURCE_IDS = [int(x) for x in env_list("IMPORT_DAILY_SOURCE_IDS") if x.isdigit()]

# Ersatz-Thema für importierte Artikel bzw. Events ohne Themen (Tag-IDs, leer = keins)
IMPORT_FALLBACK_ARTICLE_TAG_ID = env_int("IMPORT_FALLBACK_ARTICLE_TAG_ID", 0) or None
IMPORT_FALLBACK_EVENT_TAG_ID = env_int("IMPORT_FALLBACK_EVENT_TAG_ID", 0) or None

# Quellen, deren Artikel die Duplikatbereinigung nie löscht (kommagetrennte Source-IDs)
DUPLICATE_PROTECTED_SOURCE_IDS = [int(x) for x in env_list("DUPLICATE_PROTECTED_SOURCE_IDS") if x.isdigit()]

# Quellen, deren eingereichte, noch unveröffentlichte Events ohne Tags über
# /api/v4/events/preview/ öffentlich abrufbar sind (kommagetrennte Source-IDs,
# leer = Vorschau liefert nichts)
EVENT_PREVIEW_SOURCE_IDS = [int(x) for x in env_list("EVENT_PREVIEW_SOURCE_IDS") if x.isdigit()]

# Willkommensbeitrag für neu angelegte Gebiete: Quelle (leer = kein Beitrag), Tag optional
AREA_WELCOME_SOURCE_ID = env_int("AREA_WELCOME_SOURCE_ID", 0) or None
AREA_WELCOME_TAG_ID = env_int("AREA_WELCOME_TAG_ID", 0) or None

# Benutzer-ID für die Schreibprüfung des Monitorings (leer = Prüfung aus)
MONITORING_USER_ID = env_int("MONITORING_USER_ID", 0) or None


# --------------------------------------------------------------------------
# Push-Nachrichten (Firebase Cloud Messaging, optional)
# --------------------------------------------------------------------------
ENABLE_PUSH = env_bool("ENABLE_PUSH", False)
FIREBASE_SERVICE_ACCOUNT_KEY = env("FIREBASE_SERVICE_ACCOUNT_KEY")
# Zugangscode für POST /api/v4/admin/test-push/ (leer = Endpunkt gesperrt)
PUSH_TEST_ACCESS_CODE = env("PUSH_TEST_ACCESS_CODE")

if ENABLE_PUSH and FIREBASE_SERVICE_ACCOUNT_KEY and os.path.isfile(FIREBASE_SERVICE_ACCOUNT_KEY):
    import firebase_admin
    from firebase_admin import credentials

    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_KEY))


# --------------------------------------------------------------------------
# Celery
# --------------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")


# --------------------------------------------------------------------------
# Lokale Sprachmodelle (Ollama), siehe molo/llm.py
# --------------------------------------------------------------------------
LLM_BASE_URL = env("LLM_BASE_URL", "http://localhost:11434")
LLM_MODEL = env("LLM_MODEL", "gpt-oss:20b")
LLM_CODEGEN_MODEL = env("LLM_CODEGEN_MODEL", "")
LLM_THINK = env("LLM_THINK", "low")
LLM_TIMEOUT = env_int("LLM_TIMEOUT", 120)
LLM_NUM_CTX = env_int("LLM_NUM_CTX", 8192)

# Spracheingabe der Event-Suche (lokales Whisper über faster-whisper, optional)
SPEECH_TO_TEXT_ENABLED = env_bool("SPEECH_TO_TEXT_ENABLED", False)
WHISPER_MODEL = env("WHISPER_MODEL", "small")
WHISPER_DEVICE = env("WHISPER_DEVICE", "auto")


# --------------------------------------------------------------------------
# KI-Event-Suche
# --------------------------------------------------------------------------
USE_RAG_EVENT_SEARCH = env_bool("USE_RAG_EVENT_SEARCH", False)
RAG_FALLBACK_TO_TRADITIONAL = True
RAG_CACHE_ENABLED = True
RAG_CACHE_TIMEOUT = env_int("RAG_CACHE_TIMEOUT", 120)
RAG_SEARCH_DEBUG = env_bool("RAG_SEARCH_DEBUG", False)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "default-cache",
    },
    "source_check": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "source_check_cache",
        "TIMEOUT": 600,
        "OPTIONS": {"MAX_ENTRIES": 100},
    },
    "rag_search": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "rag_search_cache",
        "TIMEOUT": RAG_CACHE_TIMEOUT,
        "OPTIONS": {"MAX_ENTRIES": 1000},
    },
}


# --------------------------------------------------------------------------
# Editor / Admin-Oberfläche
# --------------------------------------------------------------------------
CKEDITOR_5_CONFIGS = {
    "default": {
        "language": "de",
        "toolbar": ["bold", "italic", "|", "bulletedList", "numberedList", "|",
                    "sourceEditing", "removeFormat"],
    },
    "extends": {
        "language": "de",
        "toolbar": ["bold", "italic", "|", "bulletedList", "numberedList", "|",
                    "blockQuote", "link", "|", "sourceEditing", "removeFormat"],
        "list": {"properties": {"styles": True, "startIndex": True, "reversed": True}},
    },
}
# Nur Rasterbilder (SVG kann Script enthalten)
CKEDITOR_5_UPLOAD_FILE_TYPES = ["jpeg", "png", "gif", "bmp", "webp"]

JET_SIDE_MENU_ITEMS = [
    {"app_label": "content", "items": [
        {"name": "article"},
        {"name": "articledraft"},
        {"name": "eventv4"},
        {"name": "source"},
        {"name": "user"},
        {"name": "organization"},
        {"name": "category"},
        {"name": "tagcategories"},
        {"name": "tag"},
        {"name": "subtag"},
        {"name": "appuser"},
        {"name": "area"},
    ]},
    {"app_label": "auth", "items": [{"name": "group"}]},
    {"label": "Tools", "items": [
        {"label": "Quellen-Link-Check",
         "url": {"type": "reverse", "name": "Moloadmin:check_source_urls"},
         "permissions": ["content.change_source"]},
    ]},
]


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{levelname} {asctime} {module} {message}", "style": "{"},
        "simple": {"format": "{levelname} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"level": "INFO", "class": "logging.StreamHandler", "formatter": "simple"},
        "file": {
            "level": "INFO",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "molo.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console", "file"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "molonews": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "molo.llm": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "import_articles": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "ai_tagging": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "rag_search": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "push_notifications": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
    },
}
