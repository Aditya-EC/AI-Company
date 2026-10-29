"""Phase 8 operations: health/readiness, metrics, worker heartbeats, preflight, retention, Celery path."""
import io
import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditEvent, Notification
from apps.orchestration import engine
from apps.orchestration.models import WorkerHeartbeat
from apps.work import services
from apps.work.models import Task

from .conftest import PASSWORD, make_task


def test_liveness_is_public_but_readiness_and_metrics_are_protected(client, db, monkeypatch, founder):
    assert client.get("/healthz").json()["database"] == "ok"
    assert client.get("/healthz/ready").status_code == 401
    assert client.get("/metrics").status_code == 401
    monkeypatch.setenv("VCOOS_HEALTH_TOKEN", "s3cret-token")
    assert client.get("/healthz/ready", HTTP_AUTHORIZATION="Bearer wrong").status_code == 401
    r = client.get("/healthz/ready", HTTP_AUTHORIZATION="Bearer s3cret-token")
    assert r.status_code in (200, 503) and set(r.json()["checks"]) == {
        "database", "migrations", "cache", "channel_layer", "workers", "disk", "providers", "company"}
    client.login(username="founder", password=PASSWORD)
    assert client.get("/metrics").status_code == 200


def test_worker_check_detects_missing_workers(client, founder, dept, employee, company):
    client.login(username="founder", password=PASSWORD)
    services.queue_task(make_task(dept, employee))
    checks = client.get("/healthz/ready").json()["checks"]
    assert not checks["workers"]["ok"] and "no live engine workers" in checks["workers"]["detail"]
    engine.heartbeat("w1")
    r = client.get("/healthz/ready")
    assert r.json()["checks"]["workers"]["ok"]
    assert r.json()["checks"]["database"]["ok"] and r.json()["checks"]["migrations"]["ok"]
    WorkerHeartbeat.objects.update(last_seen=timezone.now() - timedelta(minutes=5))
    assert not client.get("/healthz/ready").json()["checks"]["workers"]["ok"]  # stale heartbeat = dead worker


def test_emergency_stop_makes_readiness_fail(client, founder, company):
    client.login(username="founder", password=PASSWORD)
    company.emergency_stop = True
    company.save()
    r = client.get("/healthz/ready")
    assert r.status_code == 503 and r.json()["checks"]["company"]["detail"] == "emergency stop ACTIVE"


def test_metrics_content(client, founder, dept, employee, company):
    t = make_task(dept, employee)
    services.queue_task(t)
    engine.heartbeat("w1")
    client.login(username="founder", password=PASSWORD)
    body = client.get("/metrics").content.decode()
    assert 'vcoos_tasks{status="queued"} 1' in body
    assert "vcoos_workers_alive 1" in body and "vcoos_emergency_stop 0" in body
    assert "# TYPE vcoos_model_calls_total counter" in body


@pytest.mark.django_db(transaction=True)
def test_run_engine_records_heartbeat(company):
    call_command("run_engine", "--once", stdout=io.StringIO())
    hb = WorkerHeartbeat.objects.get()
    assert hb.stopped and hb.kind == "engine"  # graceful shutdown recorded


def test_preflight_flags_unsafe_configuration(db, founder, settings):
    out = io.StringIO()
    settings.SECRET_KEY = "short"
    with pytest.raises(CommandError):
        call_command("vcoos_preflight", stdout=out)
    text = out.getvalue()
    assert "[FAIL] SECRET_KEY strength" in text and "[PASS] Credential encryption key" in text
    assert "[PASS] Audit chain" in text and "[PASS] Health: migrations" in text


def test_retention_removes_only_ephemeral_data(db, founder, dept, employee, settings, company):
    old = timezone.now() - timedelta(days=200)
    read_old = Notification.objects.create(user=founder, kind="x", title="old read", is_read=True)
    unread_old = Notification.objects.create(user=founder, kind="x", title="old unread", is_read=False)
    Notification.objects.filter(pk__in=[read_old.pk, unread_old.pk]).update(created_at=old)
    task = make_task(dept, employee, status=Task.COMPLETED, completed_at=old)
    ws = settings.VCOOS_WORKSPACE_ROOT / f"task-{task.pk}"
    ws.mkdir(parents=True)
    (ws / "f.txt").write_text("x")
    from apps.reports.models import ReportJob
    settings.VCOOS_REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    job = ReportJob.objects.create(kind="company", fmt="csv", requested_by=founder, status="done", file_name="report-old.csv")
    ReportJob.objects.filter(pk=job.pk).update(created_at=old)
    (settings.VCOOS_REPORT_ROOT / "report-old.csv").write_text("x")
    audits = AuditEvent.objects.count()
    out = io.StringIO()
    call_command("apply_retention", "--dry-run", stdout=out)
    assert "notifications=1" in out.getvalue() and ws.exists() and Notification.objects.filter(pk=read_old.pk).exists()
    call_command("apply_retention", stdout=io.StringIO())
    assert not Notification.objects.filter(pk=read_old.pk).exists() and Notification.objects.filter(pk=unread_old.pk).exists()
    assert not ws.exists() and not (settings.VCOOS_REPORT_ROOT / "report-old.csv").exists()
    assert Task.objects.filter(pk=task.pk).exists()  # history kept
    assert AuditEvent.objects.count() == audits + 1  # only the retention event was added; nothing removed


@pytest.mark.django_db(transaction=True)
def test_celery_transport_executes_tasks(llm, employee, dept, company, settings):
    from config.celery import app
    from apps.orchestration import tasks as celery_tasks
    app.conf.task_always_eager = True
    try:
        llm.script = [llm.ollama(json.dumps({"action": "final", "output": "via celery", "summary": "ok"}))]
        t = make_task(dept, employee)
        services.queue_task(t)
        celery_tasks.engine_tick()
        t.refresh_from_db()
        assert t.status == Task.UNDER_REVIEW and t.output.startswith("via celery")
        assert WorkerHeartbeat.objects.filter(kind="celery").exists()
    finally:
        app.conf.task_always_eager = False


def test_dashboard_shows_worker_status(client, founder, company):
    client.login(username="founder", password=PASSWORD)
    assert b"No engine worker running" in client.get(reverse("dashboard")).content
    engine.heartbeat("w1")
    assert b"1 engine worker live" in client.get(reverse("dashboard")).content


def test_encryption_key_rotation(db, monkeypatch, settings, llm):
    from cryptography.fernet import Fernet
    from .conftest import make_provider
    p = make_provider("Cloud", llm.url, kind="openai")
    p.set_api_key("sk-original-secret-value")
    p.save()
    old_key, new_key = settings.VCOOS_ENCRYPTION_KEY, Fernet.generate_key().decode()
    settings.VCOOS_ENCRYPTION_KEY = new_key
    monkeypatch.setenv("VCOOS_OLD_ENCRYPTION_KEY", old_key)
    call_command("rotate_encryption_key", stdout=io.StringIO())
    p.refresh_from_db()
    assert p.get_api_key() == "sk-original-secret-value"  # readable with the new key only
    settings.VCOOS_ENCRYPTION_KEY = old_key
    with pytest.raises(Exception):
        p.get_api_key()
    settings.VCOOS_ENCRYPTION_KEY = new_key
    call_command("rotate_encryption_key", stdout=io.StringIO())  # idempotent
    assert AuditEvent.objects.filter(action="security.encryption_key_rotated").count() == 2
