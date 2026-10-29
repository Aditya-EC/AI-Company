"""Authorization, authentication, WebSocket access control, audit integrity, sandbox isolation, backups."""
import pytest
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.core import audit, backup
from apps.core.models import AuditEvent, CompanySettings, PermissionAssignment
from apps.core.permissions import can, require, scope_queryset
from apps.tools import sandbox
from apps.tools.registry import ToolContext, ToolError, execute
from apps.work.models import ApprovalRequest, Task
from apps.work import services

from .conftest import PASSWORD, grant, make_task


# ----------------------------------------------------------------- role permissions

def test_founder_has_all_permissions(founder):
    for p in ("manage_settings", "manage_models", "emergency_stop", "view_tasks", "review_tasks"):
        assert can(founder, p)


def test_ceo_cannot_use_founder_exclusive_permissions(ceo):
    assert can(ceo, "manage_tasks") and can(ceo, "review_tasks") and can(ceo, "view_audit")
    for p in ("manage_settings", "manage_models", "manage_tools", "manage_permissions", "emergency_stop", "manage_budgets", "appoint_ceo"):
        assert not can(ceo, p)


def test_staff_default_deny_and_scoped_grants(staff, dept, other_dept, employee):
    assert not can(staff, "view_tasks")
    PermissionAssignment.objects.create(user=staff, permission="view_tasks", department=dept)
    mine, theirs = make_task(dept, employee), make_task(other_dept)
    assert can(staff, "view_tasks", mine) and not can(staff, "view_tasks", theirs)
    assert list(scope_queryset(staff, "view_tasks", Task.objects.all())) == [mine]
    assert not can(staff, "manage_settings")  # never grantable to staff
    with pytest.raises(PermissionDenied):
        require(staff, "manage_tasks", mine)


def test_inactive_user_has_no_permissions(founder):
    founder.is_active = False
    assert not can(founder, "view_company")


# ----------------------------------------------------------------- HTTP

def test_anonymous_redirected_and_api_401(client, db):
    r = client.get("/tasks/")
    assert r.status_code == 302 and "/accounts/login/" in r["Location"]
    assert client.get("/api/office/state/").status_code == 401
    assert client.get("/accounts/login/").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_staff_cannot_open_other_department_task_or_reports(client, staff, dept, other_dept, employee):
    PermissionAssignment.objects.create(user=staff, permission="view_tasks", department=dept)
    client.login(username="staff", password=PASSWORD)
    mine, theirs = make_task(dept, employee), make_task(other_dept)
    assert client.get(reverse("task_detail", args=[mine.pk])).status_code == 200
    assert client.get(reverse("task_detail", args=[theirs.pk])).status_code == 403
    assert client.get(reverse("audit_log")).status_code == 403
    assert client.get(reverse("budgets")).status_code == 403
    assert client.get(reverse("dashboard")).status_code == 403
    r = client.post(reverse("task_action", args=[mine.pk]), {"action": "cancel"})
    assert r.status_code == 403  # viewing does not imply control


def test_ceo_cannot_trigger_sensitive_founder_actions(client, ceo, company):
    client.login(username="ceo", password=PASSWORD)
    assert client.post(reverse("company_settings"), {"action": "enable_paid"}).status_code == 403
    assert client.post(reverse("company_settings"), {"action": "stop", "reason": "x"}).status_code == 403
    assert client.get(reverse("users")).status_code == 403
    company.refresh_from_db()
    assert not company.paid_models_enabled and not company.emergency_stop


def test_founder_emergency_stop_is_audited(client, founder, company):
    client.login(username="founder", password=PASSWORD)
    r = client.post(reverse("company_settings"), {"action": "stop", "reason": "suspicious tool use"})
    assert r.status_code == 302
    company.refresh_from_db()
    assert company.emergency_stop
    assert AuditEvent.objects.filter(action="company.emergency_stop", actor_user=founder).exists()


def test_login_rate_limit(client, founder, settings):
    settings.VCOOS_LOGIN_RATE_LIMIT = 3
    for _ in range(3):
        client.post(reverse("login"), {"username": "founder", "password": "wrong"})
    r = client.post(reverse("login"), {"username": "founder", "password": PASSWORD})
    assert r.status_code == 429  # locked even with the right password
    assert AuditEvent.objects.filter(action="auth.rate_limited").exists()


def test_csrf_enforced(founder, company):
    from django.test import Client
    c = Client(enforce_csrf_checks=True)
    c.login(username="founder", password=PASSWORD)
    assert c.post(reverse("company_settings"), {"action": "stop", "reason": "x"}).status_code == 403


def test_api_keys_never_rendered(client, founder, llm, company):
    from .conftest import make_provider
    p = make_provider("Cloud", llm.url, kind="openai")
    p.set_api_key("sk-live-SECRET1234567890abcdef")
    p.save()
    client.login(username="founder", password=PASSWORD)
    for url in (reverse("provider_list"), reverse("provider_detail", args=[p.pk])):
        body = client.get(url).content.decode()
        assert "SECRET1234567890" not in body and p.encrypted_api_key not in body


def test_audit_redacts_secrets(db):
    ev = audit.record("test.event", details={"api_key": "abc", "note": "token sk-abcdefghijklmnopqrstuvwx used"})
    assert ev.details["api_key"] == "[REDACTED]" and "sk-abcdef" not in ev.details["note"]


# ----------------------------------------------------------------- WebSocket

async def _connect(user):
    # Inject the authenticated user as Channels' AuthMiddleware would.
    from apps.office.consumers import ActivityConsumer
    comm = WebsocketCommunicator(ActivityConsumer.as_asgi(), "/ws/activity/")
    comm.scope["user"] = user
    return comm, await comm.connect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_websocket_requires_authorization(dept):
    from django.contrib.auth.models import AnonymousUser
    from apps.core.models import User
    comm, (connected, _) = await _connect(AnonymousUser())
    assert not connected
    staff = await sync_to_async(User.objects.create_user)("s2", password=PASSWORD, account_type="staff")
    comm, (connected, _) = await _connect(staff)
    assert not connected  # no view_monitor grant
    await sync_to_async(PermissionAssignment.objects.create)(user=staff, permission="view_monitor", department=dept)
    comm, (connected, _) = await _connect(staff)
    assert connected
    from apps.work.events import COMPANY_GROUP
    from channels.layers import get_channel_layer
    layer = get_channel_layer()
    await layer.group_send(COMPANY_GROUP, {"type": "activity.event", "event": "task", "payload": {"department_id": dept.pk + 999, "code": "T-x"}})
    await layer.group_send(COMPANY_GROUP, {"type": "activity.event", "event": "task", "payload": {"department_id": dept.pk, "code": "T-ok"}})
    msg = await comm.receive_json_from(timeout=2)
    assert msg["payload"]["code"] == "T-ok"  # other department's event was filtered out
    await comm.disconnect()


# ----------------------------------------------------------------- tools & sandbox

def test_workspace_path_traversal_blocked(employee, dept, tools, company):
    grant(tools["workspace.write_file"], employee=employee)
    grant(tools["workspace.read_file"], employee=employee)
    task = make_task(dept, employee)
    ctx = ToolContext(task=task, employee=employee, execution=None)
    for bad in ("../escape.txt", "/etc/passwd", "a/../../b"):
        ok, out, _ = execute("workspace.write_file", {"path": bad, "content": "x"}, ctx)
        assert not ok and "workspace" in out["error"]
    ok, out, _ = execute("workspace.read_file", {"path": "../../../../etc/passwd"}, ctx)
    assert not ok


def test_tool_input_schema_validated(employee, dept, tools, company):
    grant(tools["workspace.write_file"], employee=employee)
    ctx = ToolContext(task=make_task(dept, employee), employee=employee, execution=None)
    with pytest.raises(ToolError):
        execute("workspace.write_file", {"path": "a.txt"}, ctx)
    with pytest.raises(ToolError):
        execute("workspace.write_file", {"path": "a.txt", "content": "x", "mode": "rwx"}, ctx)


def test_execute_refuses_without_permission(employee, dept, tools, company):
    ctx = ToolContext(task=make_task(dept, employee), employee=employee, execution=None)
    with pytest.raises(ToolError, match="permission denied"):
        execute("python.run", {"code": "print(1)"}, ctx)


def test_sandbox_runs_code(tmp_path):
    r = sandbox.run_python("import math\nprint(math.sqrt(16))\nopen('out.txt','w').write('ok')", tmp_path)
    assert r["exit_code"] == 0 and r["stdout"].strip() == "4.0"
    assert (tmp_path / "out.txt").read_text() == "ok"


@pytest.mark.parametrize("code", [
    "import socket",
    "open('/etc/hostname').read()",
    "open('../outside.txt','w').write('x')",
    "import os; os.system('echo hi')",
    "import os; os.listdir('/')",
    "import subprocess",
    "import ctypes",
])
def test_sandbox_blocks_escape(tmp_path, code):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = sandbox.run_python(code, ws)
    assert r["exit_code"] != 0 and "sandbox" in r["stderr"]
    assert not (tmp_path / "outside.txt").exists()


def test_sandbox_has_no_credentials_in_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    r = sandbox.run_python("import os; print(sorted(os.environ))", tmp_path)
    assert "OPENAI_API_KEY" not in r["stdout"]


def test_sandbox_timeout(tmp_path):
    r = sandbox.run_python("while True: pass", tmp_path, wall_seconds=2, cpu_seconds=1)
    assert r["exit_code"] != 0


# ----------------------------------------------------------------- audit chain & backups

def test_audit_chain_detects_tampering(db):
    for i in range(5):
        audit.record("test.event", details={"i": i})
    assert audit.verify_chain() == (True, None)
    with pytest.raises(PermissionError):
        AuditEvent.objects.filter(action="test.event").delete()
    with pytest.raises(PermissionError):
        AuditEvent.objects.first().save()
    target = AuditEvent.objects.order_by("id")[2]
    from django.db import connection
    with connection.cursor() as cur:  # simulate an attacker editing the table directly
        cur.execute("UPDATE core_auditevent SET details = %s WHERE id = %s", ['{"i": 99}', target.id])
    ok, bad = audit.verify_chain()
    assert not ok and bad == target.id


def _vendor():
    from django.db import connection
    return connection.vendor


@pytest.mark.django_db(transaction=True)
def test_backup_create_verify_and_tamper_detection(tmp_path):
    if _vendor() != "sqlite":
        pytest.skip("SQLite backup path")
    audit.record("before.backup")
    out, manifest = backup.create_backup(tmp_path)
    assert backup.verify_backup(out) == (True, "ok")
    import sqlite3
    con = sqlite3.connect(out)
    assert con.execute("SELECT count(*) FROM core_auditevent").fetchone()[0] >= 1
    con.close()
    with open(out, "ab") as f:
        f.write(b"tamper")
    assert backup.verify_backup(out)[0] is False


@pytest.mark.django_db(transaction=True)
def test_restore_from_backup(tmp_path):
    if _vendor() != "sqlite":
        pytest.skip("SQLite restore path")
    audit.record("snapshot.marker")
    out, _ = backup.create_backup(tmp_path)
    target = tmp_path / "restored.sqlite3"
    backup.restore_sqlite(out, target=target)
    import sqlite3
    con = sqlite3.connect(target)
    assert con.execute("SELECT count(*) FROM core_auditevent WHERE action='snapshot.marker'").fetchone()[0] == 1
    con.close()


@pytest.mark.django_db(transaction=True)
def test_postgres_backup_restore_rehearsal(tmp_path):
    """Full rehearsal: dump, verify, detect tampering, restore into a scratch database, verify data and audit chain."""
    if _vendor() != "postgresql":
        pytest.skip("runs when the suite is executed against PostgreSQL")
    import subprocess
    from django.conf import settings as dj
    from django.db import connection
    for i in range(3):
        audit.record("rehearsal.event", details={"i": i})
    expected = AuditEvent.objects.count()
    out, manifest = backup.create_backup(tmp_path)
    assert manifest["vendor"] == "postgresql" and backup.verify_backup(out) == (True, "ok")
    db = dj.DATABASES["default"]
    env = {"PGPASSWORD": db["PASSWORD"], "PATH": "/usr/bin:/bin:/usr/lib/postgresql/16/bin"}
    conn = ["-h", db["HOST"], "-p", str(db["PORT"]), "-U", db["USER"]]
    scratch = f"{connection.settings_dict['NAME']}_restore"
    subprocess.run(["dropdb", "--if-exists", *conn, scratch], env=env, check=True)
    subprocess.run(["createdb", *conn, scratch], env=env, check=True)
    try:
        backup.restore_postgres(out, target_db=scratch)
        count = subprocess.run(["psql", *conn, "-d", scratch, "-tAc", "select count(*) from core_auditevent"],
                               env=env, capture_output=True, text=True, check=True).stdout.strip()
        assert int(count) == expected
        # Verify the restored audit chain with the application's own hashing code.
        import psycopg
        with psycopg.connect(host=db["HOST"], port=db["PORT"], user=db["USER"], password=db["PASSWORD"], dbname=scratch) as pg:
            rows = pg.execute("select created_at, actor_label, action, target_type, target_id, details, prev_hash, hash "
                              "from core_auditevent order by id").fetchall()
        prev = audit.GENESIS
        for created_at, actor, action, ttype, tid, details, prev_hash, h in rows:
            ev = AuditEvent(created_at=created_at, actor_label=actor, action=action, target_type=ttype, target_id=tid,
                            details=details, prev_hash=prev_hash)
            assert prev_hash == prev and ev.compute_hash() == h
            prev = h
        assert len(rows) == expected
    finally:
        subprocess.run(["dropdb", "--if-exists", "--force", *conn, scratch], env=env)
    with open(out, "r+b") as f:  # truncate the archive: verification must fail
        f.truncate(out.stat().st_size // 2)
    assert backup.verify_backup(out)[0] is False


# ----------------------------------------------------------------- approvals

def test_approval_workflow(founder, ceo, staff, employee, dept, company):
    task = make_task(dept, employee, requires_approval=True)
    services.queue_task(task)
    assert task.status == Task.AWAITING_APPROVAL
    ap = ApprovalRequest.objects.get(task=task)
    with pytest.raises(services.InvalidTransition):
        services.decide_approval(ap, user=staff, approve=True)
    services.decide_approval(ap, user=ceo, approve=True, note="ok")
    task.refresh_from_db()
    assert task.status == Task.QUEUED
    assert AuditEvent.objects.filter(action="approval.decided", actor_user=ceo).exists()


def test_founder_only_approval_rejects_ceo(founder, ceo, employee, dept):
    task = make_task(dept, employee)
    ap = ApprovalRequest.objects.create(kind="deployment", title="Deploy", task=task, required_authority="founder", requested_by_label="x")
    with pytest.raises(services.InvalidTransition):
        services.decide_approval(ap, user=ceo, approve=True)
    services.decide_approval(ap, user=founder, approve=True)
    assert ap.status == ApprovalRequest.APPROVED


def test_rejected_approval_cancels_task(ceo, employee, dept):
    task = make_task(dept, employee, requires_approval=True)
    services.queue_task(task)
    services.decide_approval(ApprovalRequest.objects.get(task=task), user=ceo, approve=False, note="too costly")
    task.refresh_from_db()
    assert task.status == Task.CANCELLED


def test_author_cannot_review_own_work(llm, dept, company, local_model):
    from apps.core.models import EmployeeProfile, User
    u = User.objects.create_user("agentacct", password=PASSWORD, account_type="staff")
    emp = EmployeeProfile.objects.create(name="Self", job_title="Dev", department=dept, user_account=u)
    task = make_task(dept, emp, status=Task.UNDER_REVIEW)
    with pytest.raises(services.InvalidTransition):
        services.review_task(task, user=u, outcome="accept")


# ----------------------------------------------------------------- Phase 8 hardening

def test_csp_nonce_on_every_inline_script(client, founder, company):
    import re
    client.login(username="founder", password=PASSWORD)
    for url in ("/", "/office/", "/monitor/", "/accounts/password/"):
        r = client.get(url)
        csp = r["Content-Security-Policy"]
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        assert "object-src 'none'" in csp and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0]
        body = r.content.decode()
        inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>", body)
        assert inline and all(f'nonce="{nonce}"' in attrs for attrs in inline), url
        assert "onclick=" not in body
    assert client.get("/").headers["Content-Security-Policy"] != r["Content-Security-Policy"]  # fresh nonce per request


def test_password_reset_rate_limited_without_enumeration(client, db, founder, settings):
    settings.VCOOS_LOGIN_RATE_LIMIT = 2
    from django.core import mail
    for _ in range(4):
        r = client.post(reverse("password_reset"), {"email": founder.email})
        assert r.status_code == 302 and r["Location"].endswith("/accounts/password-reset/done/")
    assert len(mail.outbox) == 2  # further requests silently dropped
    assert AuditEvent.objects.filter(action="auth.password_reset_rate_limited").count() == 2


def test_colour_fields_validated(db, dept):
    from django.core.exceptions import ValidationError
    dept.color = 'red" onmouseover="x'
    with pytest.raises(ValidationError):
        dept.full_clean()


def test_sandbox_with_os_network_namespace(tmp_path, monkeypatch):
    """The documented OS-level wrapper runs agent code with no network at the kernel level."""
    import shutil
    import subprocess
    if not shutil.which("unshare") or subprocess.run(["unshare", "--net", "--map-root-user", "true"]).returncode != 0:
        pytest.skip("unprivileged network namespaces unavailable")
    monkeypatch.setenv("VCOOS_SANDBOX_WRAPPER", "unshare --net --map-root-user")
    r = sandbox.run_python("print(sum(range(10)))", tmp_path)
    assert r["exit_code"] == 0 and r["stdout"].strip() == "45"
    r = sandbox.run_python("import socket", tmp_path)
    assert r["exit_code"] != 0  # in-process hook still applies inside the namespace
