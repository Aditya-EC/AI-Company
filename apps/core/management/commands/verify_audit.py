from django.core.management.base import BaseCommand, CommandError

from apps.core.audit import verify_chain
from apps.core.models import AuditEvent


class Command(BaseCommand):
    help = "Verify the tamper-evident audit hash chain."

    def handle(self, *a, **kw):
        ok, bad = verify_chain()
        if not ok:
            raise CommandError(f"Audit chain BROKEN at event {bad}")
        self.stdout.write(self.style.SUCCESS(f"Audit chain intact ({AuditEvent.objects.count()} events)."))
