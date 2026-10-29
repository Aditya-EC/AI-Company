"""Production readiness checks. Exit code 1 if any FAIL."""
import os
import shutil

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.core import health


class Command(BaseCommand):
    help = "Check that this installation is configured safely for production."

    def handle(self, *a, **kw):
        results = []

        def add(level, name, detail):
            results.append((level, name, detail))

        add("PASS" if not settings.DEBUG else "FAIL", "DEBUG disabled", f"DEBUG={settings.DEBUG}")
        key = settings.SECRET_KEY or ""
        add("PASS" if len(key) >= 50 and "insecure" not in key else "FAIL", "SECRET_KEY strength", f"{len(key)} characters")
        try:
            from cryptography.fernet import Fernet

            Fernet(settings.VCOOS_ENCRYPTION_KEY.encode())
            add("PASS", "Credential encryption key", "valid Fernet key")
        except Exception:
            add("FAIL", "Credential encryption key", "VCOOS_ENCRYPTION_KEY missing or invalid")
        hosts = settings.ALLOWED_HOSTS
        add("PASS" if hosts and "*" not in hosts else "FAIL", "ALLOWED_HOSTS", ", ".join(hosts) or "empty")
        vendor = settings.DATABASES["default"]["ENGINE"].rsplit(".", 1)[-1]
        add("PASS" if vendor == "postgresql" else "WARN", "Database", f"{vendor} (PostgreSQL recommended for production)")
        add("PASS" if settings.REDIS_URL else "WARN", "Redis", "configured" if settings.REDIS_URL else
            "not set: live updates are single-process and login rate limits are per-process")
        add("PASS" if os.environ.get("VCOOS_SANDBOX_WRAPPER") else "WARN", "OS-level sandbox",
            os.environ.get("VCOOS_SANDBOX_WRAPPER", "VCOOS_SANDBOX_WRAPPER not set: agent code relies on in-process isolation only"))
        add("PASS" if os.environ.get("VCOOS_HEALTH_TOKEN") else "WARN", "Health/metrics token",
            "set" if os.environ.get("VCOOS_HEALTH_TOKEN") else "not set: /healthz/ready and /metrics only for Founder sessions")
        add("PASS" if shutil.which("git") else "WARN", "git", "available" if shutil.which("git") else "missing: Software Factory repositories disabled")
        if settings.DATABASES["default"]["ENGINE"].endswith("postgresql"):
            add("PASS" if shutil.which("pg_dump") and shutil.which("pg_restore") else "FAIL", "pg_dump/pg_restore", "needed for backups")
        for name, path in (("Workspace root", settings.VCOOS_WORKSPACE_ROOT), ("Backup root", settings.VCOOS_BACKUP_ROOT),
                           ("Report root", settings.VCOOS_REPORT_ROOT)):
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".preflight"
                probe.write_text("ok")
                probe.unlink()
                add("PASS", name, str(path))
            except OSError as exc:
                add("FAIL", name, f"{path}: {exc}")
        for check in ("database", "migrations", "cache"):
            r = health._check(health.CHECKS[check])
            add("PASS" if r["ok"] else "FAIL", f"Health: {check}", r["detail"])
        from apps.core.models import CompanySettings, User

        add("PASS" if User.objects.filter(account_type=User.FOUNDER, is_active=True).exists() else "FAIL", "Founder account", "")
        c = CompanySettings.get()
        if c.email_notifications_enabled:
            console = settings.EMAIL_BACKEND.endswith("console.EmailBackend")
            add("FAIL" if console else "PASS", "Email backend", settings.EMAIL_BACKEND)
        try:
            call_command("check", "--deploy", "--fail-level", "ERROR", stdout=open(os.devnull, "w"))
            add("PASS", "Django deploy checks", "no errors")
        except Exception as exc:
            add("FAIL", "Django deploy checks", str(exc)[:200])
        try:
            call_command("verify_audit", stdout=open(os.devnull, "w"))
            add("PASS", "Audit chain", "intact")
        except CommandError as exc:
            add("FAIL", "Audit chain", str(exc))

        style = {"PASS": self.style.SUCCESS, "WARN": self.style.WARNING, "FAIL": self.style.ERROR}
        for level, name, detail in results:
            self.stdout.write(style[level](f"[{level}] {name}") + (f" - {detail}" if detail else ""))
        fails = sum(1 for r in results if r[0] == "FAIL")
        warns = sum(1 for r in results if r[0] == "WARN")
        self.stdout.write(f"{fails} failed, {warns} warnings, {len(results) - fails - warns} passed")
        if fails:
            raise CommandError("Preflight failed")
