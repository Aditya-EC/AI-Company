"""Operational health checks and Prometheus metrics.

/healthz            public liveness: database reachable (no details)
/healthz/ready      readiness detail  - Founder session or `Authorization: Bearer $VCOOS_HEALTH_TOKEN`
/metrics            Prometheus text   - same authorization
"""
import hmac
import os
import shutil

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Count, Sum


def authorized(request):
    if request.user.is_authenticated and request.user.is_founder:
        return True
    token = os.environ.get("VCOOS_HEALTH_TOKEN", "")
    header = request.META.get("HTTP_AUTHORIZATION", "")
    return bool(token) and hmac.compare_digest(header, f"Bearer {token}")


def _check(fn):
    try:
        ok, detail = fn()
    except Exception as exc:
        ok, detail = False, f"{exc.__class__.__name__}: {exc}"[:300]
    return {"ok": bool(ok), "detail": detail}


def check_database():
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return True, connection.vendor


def check_migrations():
    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return not pending, f"{len(pending)} unapplied" if pending else "all applied"


def check_cache():
    cache.set("vcoos-health", "1", 10)
    return cache.get("vcoos-health") == "1", settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1]


def check_channel_layer():
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    name = async_to_sync(layer.new_channel)()
    async_to_sync(layer.send)(name, {"type": "health.ping"})
    msg = async_to_sync(layer.receive)(name)
    in_memory = layer.__class__.__name__ == "InMemoryChannelLayer"
    detail = layer.__class__.__name__ + (" (single-process only; set VCOOS_REDIS_URL for cross-process events)" if in_memory else "")
    return msg.get("type") == "health.ping", detail


def check_workers():
    from apps.orchestration.models import WorkerHeartbeat
    from apps.work.models import Task

    alive = WorkerHeartbeat.alive().count()
    queued = Task.objects.filter(status=Task.QUEUED).count()
    if alive == 0 and queued:
        return False, f"no live engine workers; {queued} tasks queued"
    return True, f"{alive} live worker(s); {queued} queued"


def check_disk():
    root = settings.VCOOS_WORKSPACE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    free_pct = 100 * usage.free / usage.total
    return free_pct >= 10, f"{usage.free // (1024 ** 3)} GiB free ({free_pct:.0f}%)"


def check_providers():
    from apps.gateway.models import ModelProvider

    enabled = ModelProvider.objects.filter(is_enabled=True)
    bad = [p.name for p in enabled if p.circuit_open or p.health_status in ("down", "auth_error")]
    return not (enabled.exists() and len(bad) == enabled.count()), (f"unhealthy: {', '.join(bad)}" if bad else f"{enabled.count()} enabled")


def check_company():
    from .models import CompanySettings

    c = CompanySettings.get()
    return not c.emergency_stop, "emergency stop ACTIVE" if c.emergency_stop else "operating"


CHECKS = {"database": check_database, "migrations": check_migrations, "cache": check_cache, "channel_layer": check_channel_layer,
          "workers": check_workers, "disk": check_disk, "providers": check_providers, "company": check_company}


def readiness():
    results = {name: _check(fn) for name, fn in CHECKS.items()}
    return all(r["ok"] for r in results.values()), results


def metrics_text():
    from apps.core.models import EmployeeProfile, Incident
    from apps.gateway.models import ModelProvider, UsageRecord
    from apps.orchestration.models import WorkerHeartbeat
    from apps.work.models import ApprovalRequest, Task

    lines = []

    def metric(name, help_text, kind, samples):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lbl = ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in labels.items())
            lines.append(f"{name}{{{lbl}}} {value}" if lbl else f"{name} {value}")

    metric("vcoos_tasks", "Tasks by status", "gauge",
           [({"status": s}, n) for s, n in Task.objects.values_list("status").annotate(n=Count("id")).order_by("status")])
    metric("vcoos_employees", "AI employees by profile status", "gauge",
           [({"status": s}, n) for s, n in EmployeeProfile.objects.values_list("status").annotate(n=Count("id")).order_by("status")])
    metric("vcoos_workers_alive", "Engine workers with a recent heartbeat", "gauge", [({}, WorkerHeartbeat.alive().count())])
    usage = UsageRecord.objects.values_list("success").annotate(n=Count("id"))
    metric("vcoos_model_calls_total", "Model gateway calls", "counter", [({"success": str(s).lower()}, n) for s, n in usage])
    metric("vcoos_model_fallback_calls_total", "Successful calls served by a fallback model", "counter",
           [({}, UsageRecord.objects.filter(was_fallback=True, success=True).count())])
    cost = UsageRecord.objects.aggregate(c=Sum("estimated_cost_usd"))["c"] or 0
    metric("vcoos_estimated_cost_usd_total", "Estimated model cost (not billed)", "counter", [({}, float(cost))])
    metric("vcoos_provider_unhealthy", "Provider unhealthy (1) or not (0)", "gauge",
           [({"provider": p.name}, int(p.circuit_open or p.health_status in ("down", "auth_error"))) for p in ModelProvider.objects.all()])
    metric("vcoos_pending_approvals", "Approval requests awaiting a decision", "gauge",
           [({}, ApprovalRequest.objects.filter(status=ApprovalRequest.PENDING).count())])
    metric("vcoos_open_incidents", "Unresolved incidents", "gauge", [({}, Incident.objects.filter(is_resolved=False).count())])
    from .models import CompanySettings

    metric("vcoos_emergency_stop", "Emergency stop active", "gauge", [({}, int(CompanySettings.get().emergency_stop))])
    return "\n".join(lines) + "\n"
