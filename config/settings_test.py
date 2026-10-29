import os
import tempfile
from pathlib import Path

os.environ.setdefault("VCOOS_DEBUG", "1")
os.environ.setdefault("VCOOS_SECRET_KEY", "test-secret-key")
os.environ.pop("VCOOS_REDIS_URL", None)

from .settings import *  # noqa: E402,F401,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
VCOOS_WORKSPACE_ROOT = Path(tempfile.mkdtemp(prefix="vcoos-ws-"))
VCOOS_BACKUP_ROOT = Path(tempfile.mkdtemp(prefix="vcoos-bk-"))
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
VCOOS_ENCRYPTION_KEY = "kXr0d3Gq9m1oQe0T5C5y8w8QqvJp8m3yJ9w2xVv4bS4="
# On-disk test database so backup/restore tests exercise the real SQLite file path.
DATABASES["default"]["TEST"] = {"NAME": str(Path(tempfile.gettempdir()) / f"vcoos-test-{os.getpid()}.sqlite3")}
STORAGES = {**STORAGES, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}  # noqa: F405
