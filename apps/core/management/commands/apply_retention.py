from django.core.management.base import BaseCommand

from apps.core import retention


class Command(BaseCommand):
    help = "Apply the data-retention policy (never touches audit, task, review, decision or knowledge history)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *a, dry_run=False, **kw):
        result = retention.apply(dry_run=dry_run)
        self.stdout.write(("Would remove: " if dry_run else "Removed: ") + ", ".join(f"{k}={v}" for k, v in result.items()))
