"""Data-retention policy. Never deletes audit events, tasks, executions, reviews, decisions or knowledge
history: only ephemeral data (read notifications, export files, finished workspaces, stale heartbeats)."""
import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import audit
from .models import CompanySettings, Notification


def apply(dry_run=False):
    c = CompanySettings.get()
    now = timezone.now()
    result = {}
    notes = Notification.objects.filter(is_read=True, created_at__lt=now - timedelta(days=c.retention_notifications_days))
    result["notifications"] = notes.count()
    if not dry_run:
        notes.delete()

    from apps.reports.models import ReportJob

    jobs = ReportJob.objects.filter(created_at__lt=now - timedelta(days=c.retention_exports_days)).exclude(file_name="")
    result["report_files"] = 0
    for job in jobs:
        path = Path(settings.VCOOS_REPORT_ROOT) / job.file_name
        if path.is_file():
            result["report_files"] += 1
            if not dry_run:
                path.unlink()
        if not dry_run:
            ReportJob.objects.filter(pk=job.pk).update(file_name="", status="failed", error="file removed by retention policy")

    from apps.work.models import Task

    cutoff = now - timedelta(days=c.retention_workspaces_days)
    root = Path(settings.VCOOS_WORKSPACE_ROOT).resolve()
    result["workspaces"] = 0
    for task in Task.objects.filter(status__in=Task.TERMINAL, completed_at__lt=cutoff).filter(project__has_repository=False) | \
            Task.objects.filter(status__in=Task.TERMINAL, completed_at__lt=cutoff, project__isnull=True):
        ws = (root / f"task-{task.pk}").resolve()
        if ws.parent == root and ws.is_dir():
            result["workspaces"] += 1
            if not dry_run:
                shutil.rmtree(ws, ignore_errors=True)

    from apps.orchestration.engine import prune_heartbeats
    from apps.orchestration.models import WorkerHeartbeat

    stale = WorkerHeartbeat.objects.filter(last_seen__lt=now - timedelta(days=7))
    result["heartbeats"] = stale.count()
    if not dry_run:
        prune_heartbeats()
        audit.record("system.retention_applied", details=result)
    return result
