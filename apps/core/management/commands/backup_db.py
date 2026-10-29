from django.core.management.base import BaseCommand, CommandError

from apps.core import audit, backup


class Command(BaseCommand):
    help = "Create (and verify) a database backup, or restore one with --restore PATH (SQLite)."

    def add_arguments(self, parser):
        parser.add_argument("--dest")
        parser.add_argument("--restore")
        parser.add_argument("--verify")

    def handle(self, *a, dest=None, restore=None, verify=None, **kw):
        if verify:
            ok, why = backup.verify_backup(verify)
            if not ok:
                raise CommandError(why)
            self.stdout.write(self.style.SUCCESS("Backup verified."))
            return
        if restore:
            from django.db import connection

            if connection.vendor == "postgresql":
                target = backup.restore_postgres(restore)
                self.stdout.write(self.style.SUCCESS(f"Restored into database {target}. Run verify_audit next."))
                return
            target = backup.restore_sqlite(restore)
            self.stdout.write(self.style.SUCCESS(f"Restored into {target} (previous copy kept as .pre-restore)."))
            return
        out, manifest = backup.create_backup(dest)
        ok, why = backup.verify_backup(out)
        if not ok:
            raise CommandError(f"Backup written but failed verification: {why}")
        audit.record("system.backup", details=manifest)
        self.stdout.write(self.style.SUCCESS(f"Backup {out} sha256={manifest['sha256']}"))
