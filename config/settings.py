"""VCoOS settings. All secrets and environment-specific values come from env vars."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = env_bool("VCOOS_DEBUG", False)
SECRET_KEY = env("VCOOS_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-insecure-key-do-not-use-in-production"
    else:
        raise RuntimeError("VCOOS_SECRET_KEY must be set when VCOOS_DEBUG is off.")

ALLOWED_HOSTS = [h for h in env("VCOOS_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in env("VCOOS_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "daphne",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "apps.core",
    "apps.gateway",
    "apps.tools",
    "apps.work",
    "apps.orchestration",
    "apps.office",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.LoginRequiredMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.company",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Database: SQLite in development, PostgreSQL via VCOOS_DATABASE_URL-style vars in production.
if env("VCOOS_DB_ENGINE", "sqlite") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("VCOOS_DB_NAME", "vcoos"),
            "USER": env("VCOOS_DB_USER", "vcoos"),
            "PASSWORD": env("VCOOS_DB_PASSWORD", ""),
            "HOST": env("VCOOS_DB_HOST", "localhost"),
            "PORT": env("VCOOS_DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "ATOMIC_REQUESTS": False,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": env("VCOOS_SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
            "OPTIONS": {"timeout": 20},
        }
    }

AUTH_USER_MODEL = "core.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("VCOOS_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage" if not DEBUG
                    else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
WHITENOISE_USE_FINDERS = DEBUG
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Sessions & cookies
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 10
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_bool("VCOOS_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(env("VCOOS_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Email (password reset). Console backend in development.
EMAIL_BACKEND = env("VCOOS_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("VCOOS_EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("VCOOS_EMAIL_PORT", "25"))
EMAIL_HOST_USER = env("VCOOS_EMAIL_USER", "")
EMAIL_HOST_PASSWORD = env("VCOOS_EMAIL_PASSWORD", "")
EMAIL_USE_TLS = env_bool("VCOOS_EMAIL_TLS", False)
DEFAULT_FROM_EMAIL = env("VCOOS_FROM_EMAIL", "vcoos@localhost")

# Cache (used for login rate limiting). Redis in production, local memory in development.
REDIS_URL = env("VCOOS_REDIS_URL", "")
if REDIS_URL:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Channels: Redis layer is required for cross-process events (worker -> browser).
if REDIS_URL:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": {"hosts": [REDIS_URL]}}}
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Celery (optional). Without it, run `python manage.py run_engine` as the worker.
CELERY_BROKER_URL = env("VCOOS_CELERY_BROKER", REDIS_URL or "memory://")
CELERY_RESULT_BACKEND = None
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BEAT_SCHEDULE = {
    "engine-tick": {"task": "apps.orchestration.tasks.engine_tick", "schedule": 5.0},
}
VCOOS_EXECUTION_BACKEND = env("VCOOS_EXECUTION_BACKEND", "worker")  # "worker" or "celery"

# VCoOS-specific
VCOOS_ENCRYPTION_KEY = env("VCOOS_ENCRYPTION_KEY", "")  # Fernet key for stored provider credentials
VCOOS_WORKSPACE_ROOT = Path(env("VCOOS_WORKSPACE_ROOT", str(BASE_DIR / "var" / "workspaces")))
VCOOS_BACKUP_ROOT = Path(env("VCOOS_BACKUP_ROOT", str(BASE_DIR / "var" / "backups")))
VCOOS_LOGIN_RATE_LIMIT = int(env("VCOOS_LOGIN_RATE_LIMIT", "5"))  # attempts per window
VCOOS_LOGIN_RATE_WINDOW = int(env("VCOOS_LOGIN_RATE_WINDOW", "300"))  # seconds
VCOOS_TASK_LEASE_SECONDS = int(env("VCOOS_TASK_LEASE_SECONDS", "120"))
VCOOS_ALLOW_PRIVATE_MODEL_HOSTS = env_bool("VCOOS_ALLOW_PRIVATE_MODEL_HOSTS", True)  # Ollama on localhost

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {"format": '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "structured"}},
    "root": {"handlers": ["console"], "level": env("VCOOS_LOG_LEVEL", "INFO")},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}
