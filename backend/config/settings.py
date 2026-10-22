"""SentinelVision AI Django project settings."""

import os

# Must be set before OpenCV is imported anywhere in the process.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;8000000|rw_timeout;8000000|max_delay;500000",
)

from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.getenv("DEBUG", "True").lower() in ("true", "1", "yes")
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "drf_spectacular",
    "apps.tenancy",
    "apps.sites",
    "apps.cameras.apps.CamerasConfig",
    "apps.events",
    "apps.alerts",
    "apps.audit",
    "apps.identity",
    "apps.analytics",
    "apps.safety",
    "apps.ops",
    "apps.rules",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.audit.middleware.AuditContextMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "config.wsgi.application"

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://sentinel:sentinel@localhost:5432/sentinelvision")
if DATABASE_URL.startswith("sqlite:///"):
    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / db_path if not db_path.startswith("/") and ":" not in db_path[:3] else db_path,
        }
    }
elif DATABASE_URL.startswith("postgres://"):
    import re

    match = re.match(
        r"postgres://(?P<user>[^:]+):(?P<password>[^@]+)@(?P<host>[^:]+):(?P<port>\d+)/(?P<name>.+)",
        DATABASE_URL,
    )
    if match:
        db = match.groupdict()
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": db["name"],
                "USER": db["user"],
                "PASSWORD": db["password"],
                "HOST": db["host"],
                "PORT": db["port"],
            }
        }
    else:
        DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()
]
CORS_ALLOW_CREDENTIALS = True
CORS_EXPOSE_HEADERS = [
    "X-Detections",
    "X-Objects",
    "X-Verify-Ok",
    "X-Verify-Unknown",
    "X-Verify-Threat",
    "X-Verify-Danger",
    "X-Verify-Labels",
    "X-Verify-Issue",
    "X-Verify-Announce",
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "apps.cameras.authentication.EdgeAPIKeyAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
        "rest_framework.filters.SearchFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "SentinelVision AI API",
    "DESCRIPTION": "Enterprise AI Smart Security & Activity Monitoring Platform",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

EDGE_API_KEY = os.getenv("EDGE_API_KEY", "edge-dev-api-key-change-in-production")

# --- M6 Celery / Redis ---
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/0"))
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
# Default eager so local/tests work without Redis; compose workers set False.
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "True").lower() in ("1", "true", "yes")
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {
    "apps.ops.tasks.send_notification_job": {"queue": "notifications"},
    "apps.ops.tasks.process_recording_job": {"queue": "recordings"},
    "apps.ops.tasks.forward_siem_event": {"queue": "siem"},
}
CELERY_BEAT_SCHEDULE = {
    "drain-notification-queue": {
        "task": "apps.ops.tasks.drain_notification_queue",
        "schedule": 15.0,
    },
    "drain-recording-queue": {
        "task": "apps.ops.tasks.drain_recording_queue",
        "schedule": 20.0,
    },
    "purge-expired-evidence": {
        "task": "apps.ops.tasks.purge_expired_evidence",
        "schedule": 3600.0,
    },
}
# Camera monitor runs as a thread inside the web process, which already holds
# the detection and face models. 0 disables it. Running it from Celery loaded a
# second copy of the models in every worker process.
CAMERA_MONITOR_INTERVAL = float(os.getenv("CAMERA_MONITOR_INTERVAL", "10"))
# Always-on per-camera recording threads (camera.auto_record). Off by default to save CPU.
# Wall-clock zone for per-camera person-alert windows (server runs in UTC).
ALERT_TIME_ZONE = os.getenv("ALERT_TIME_ZONE", "Asia/Karachi")
# Shared secret for scripts/alert_player.py, which plays sounds on the host.
ALERT_PLAYER_KEY = os.getenv("ALERT_PLAYER_KEY", "alert-dev-key-change-me")
# Per-camera continuous recording (camera.auto_record) via ffmpeg stream copy — cheap on CPU.
CONTINUOUS_RECORDING = os.getenv("CONTINUOUS_RECORDING", "True").lower() in ("1", "true", "yes")

# --- M6 Evidence storage (local | s3 | minio) ---
EVIDENCE_STORAGE_BACKEND = os.getenv("EVIDENCE_STORAGE_BACKEND", "local").lower()
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "minioadmin"))
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minioadmin"))
AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME", "sentinelvision-evidence")
AWS_S3_ENDPOINT_URL = os.getenv("AWS_S3_ENDPOINT_URL", os.getenv("MINIO_ENDPOINT", ""))
AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "us-east-1")
AWS_S3_SIGNATURE_VERSION = "s3v4"
EVIDENCE_SIGNED_URL_TTL_SECONDS = int(os.getenv("EVIDENCE_SIGNED_URL_TTL_SECONDS", "3600"))
EVIDENCE_DEFAULT_TTL_HOURS = int(os.getenv("EVIDENCE_DEFAULT_TTL_HOURS", "72"))

# --- M6 Notifications ---
NOTIFICATION_RATE_LIMIT_PER_MINUTE = int(os.getenv("NOTIFICATION_RATE_LIMIT_PER_MINUTE", "60"))
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "alerts@sentinelvision.local")
SMS_PROVIDER_URL = os.getenv("SMS_PROVIDER_URL", "")
WHATSAPP_PROVIDER_URL = os.getenv("WHATSAPP_PROVIDER_URL", "")
PUSH_PROVIDER_URL = os.getenv("PUSH_PROVIDER_URL", "")
WEBHOOK_DEFAULT_TIMEOUT_SEC = int(os.getenv("WEBHOOK_DEFAULT_TIMEOUT_SEC", "10"))

# --- M6 Observability ---
PROMETHEUS_ENABLED = os.getenv("PROMETHEUS_ENABLED", "True").lower() in ("1", "true", "yes")

# --- Vision AI (free local by default — no API key) ---
VISION_LLM_PROVIDER = os.getenv("VISION_LLM_PROVIDER", "free").lower()
VISION_LLM_MODEL = os.getenv("VISION_LLM_MODEL", "llava")
VISION_LLM_BASE_URL = os.getenv("VISION_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
VISION_LLM_API_KEY = os.getenv("VISION_LLM_API_KEY", "")
VISION_LLM_TIMEOUT_SEC = int(os.getenv("VISION_LLM_TIMEOUT_SEC", "90"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}

if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
