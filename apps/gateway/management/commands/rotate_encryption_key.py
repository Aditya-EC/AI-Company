"""Re-encrypt stored provider credentials after changing VCOOS_ENCRYPTION_KEY.

Procedure: set VCOOS_ENCRYPTION_KEY to the NEW key, then run
    VCOOS_OLD_ENCRYPTION_KEY=<old key> python manage.py rotate_encryption_key
"""
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core import audit
from apps.gateway.models import ModelProvider


class Command(BaseCommand):
    help = "Re-encrypt stored provider API keys from VCOOS_OLD_ENCRYPTION_KEY to the current VCOOS_ENCRYPTION_KEY."

    def handle(self, *a, **kw):
        old_key = os.environ.get("VCOOS_OLD_ENCRYPTION_KEY", "")
        if not old_key or not settings.VCOOS_ENCRYPTION_KEY:
            raise CommandError("Set VCOOS_OLD_ENCRYPTION_KEY (old) and VCOOS_ENCRYPTION_KEY (new).")
        old, new = Fernet(old_key.encode()), Fernet(settings.VCOOS_ENCRYPTION_KEY.encode())
        n = 0
        with transaction.atomic():
            for p in ModelProvider.objects.exclude(encrypted_api_key="").select_for_update():
                try:
                    raw = old.decrypt(p.encrypted_api_key.encode())
                except InvalidToken:
                    try:
                        new.decrypt(p.encrypted_api_key.encode())
                        continue  # already on the new key
                    except InvalidToken:
                        raise CommandError(f"Provider {p.name}: credential decrypts with neither key; aborting (nothing changed)")
                p.encrypted_api_key = new.encrypt(raw).decode()
                p.save(update_fields=["encrypted_api_key"])
                n += 1
            audit.record("security.encryption_key_rotated", details={"credentials": n})
        self.stdout.write(self.style.SUCCESS(f"Re-encrypted {n} provider credential(s)."))
