"""Phase 7: knowledge base, AI meetings, reports & exports, notifications."""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Budget, CompanySettings, EmployeeProfile, Incident, Notification, NotificationPreference, PermissionAssignment
from apps.gateway.models import EmployeeModelPolicy, UsageRecord
from apps.knowledge import meetings as msvc
from apps.knowledge import services as kb
from apps.knowledge.models import KnowledgeDocument as Doc
from apps.knowledge.models import KnowledgeVersion, Meeting
from apps.orchestration import engine
from apps.reports import compute, export
from apps.work import services
from apps.work.models import Review, Task

from .conftest import PASSWORD, grant, make_model, make_provider, make_task, no_sleep

W = "p7-worker"


def run_task(task):
    services.queue_task(task)
    assert engine.claim_task(task.pk, W)
    engine.execute_task(task.pk, W, sleep=no_sleep)
    task.refresh_from_db()
    return task


def approved_doc(title, content, user, **kw):
    doc, v = kb.create_document(title=title, content=content, user=user, **{"category": "technical", **kw})
    kb.review_version(v, user=user, approve=True)
    doc.refresh_from_db()
    return doc


# ------------------------------------------------------------------ knowledge

def test_agent_contribution_is_never_authoritative_until_founder_approves(employee, founder, ceo, dept):
    doc, v = kb.create_document(title="Deploy policy", category="policy", content="Always deploy on Fridays", employee=employee,
                                access=Doc.DEPARTMENT, department=dept)
    assert doc.status == Doc.IN_REVIEW and not doc.is_authoritative
    assert not kb.agent_documents(employee).filter(pk=doc.pk).exists()
    with pytest.raises(kb.KnowledgeError, match="Only the Founder"):
        kb.review_version(v, user=ceo, approve=True)
    kb.review_version(v, user=founder, approve=True, note="reviewed")
    doc.refresh_from_db()
    assert doc.is_authoritative and kb.agent_documents(employee).filter(pk=doc.pk).exists()


def test_non_policy_review_rules(founder, ceo, dept):
    doc, v1 = kb.create_document(title="API notes", category="technical", content="v1", user=ceo)
    with pytest.raises(kb.KnowledgeError, match="authors cannot approve"):
        kb.review_version(v1, user=ceo, approve=True)
    kb.review_version(v1, user=founder, approve=True)
    v2 = kb.propose_version(doc, content="v2 text", change_note="update", user=founder)
    kb.review_version(v2, user=ceo, approve=True)
    doc.refresh_from_db()
    v1.refresh_from_db()
    assert doc.current_version == v2 and v1.status == KnowledgeVersion.SUPERSEDED
    v3 = kb.propose_version(doc, content="bad", user=founder)
    kb.review_version(v3, user=ceo, approve=False, note="inaccurate")
    doc.refresh_from_db()
    assert doc.current_version == v2  # rejected change never served


def test_access_scopes_for_staff_and_agents(founder, staff, dept, other_dept, employee):
    company = approved_doc("Company handbook", "handbook", founder)
    mine = approved_doc("Eng guide", "guide", founder, access=Doc.DEPARTMENT, department=dept)
    theirs = approved_doc("Finance guide", "ledger", founder, access=Doc.DEPARTMENT, department=other_dept)
    lead = approved_doc("Board strategy", "secret", founder, access=Doc.LEADERSHIP)
    assert not kb.visible_documents(staff).exists()  # default deny
    PermissionAssignment.objects.create(user=staff, permission="view_knowledge", department=dept)
    assert set(kb.visible_documents(staff)) == {company, mine}
    assert set(kb.agent_documents(employee)) == {company, mine}  # never leadership, never other departments
    assert lead not in kb.agent_documents(employee)
    assert theirs not in kb.visible_documents(staff)


def test_relevant_approved_knowledge_is_injected_into_prompt(llm, founder, employee, dept, company):
    approved_doc("Payment rounding standard", "Round currency to two decimals using banker's rounding.", founder)
    kb.create_document(title="Payment rounding draft", category="technical", content="Draft rule: truncate payment amounts", user=founder)
    llm.script = [llm.ollama(json.dumps({"action": "final", "output": "ok", "summary": "ok"}))]
    run_task(make_task(dept, employee, title="Implement payment rounding", description="Round payment amounts correctly"))
    system = llm.requests[0]["json"]["messages"][0]["content"]
    assert "Payment rounding standard" in system and "banker's rounding" in system
    assert "truncate payment" not in system  # unapproved drafts never reach agents


def test_knowledge_tools_respect_scope(llm, founder, employee, dept, other_dept, company, tools):
    for s in ("knowledge.search", "knowledge.read", "knowledge.propose"):
        grant(tools[s], employee=employee)
    other = approved_doc("Finance close procedure", "monthly close", founder, access=Doc.DEPARTMENT, department=other_dept)
    mine = approved_doc("Engineering close checklist", "release close", founder)
    llm.script = [llm.ollama(llm.tool("knowledge.search", {"query": "close"})),
                  llm.ollama(llm.tool("knowledge.read", {"document_id": other.pk})),
                  llm.ollama(llm.tool("knowledge.propose", {"title": "Lesson", "category": "lessons", "content": "Write tests first"})),
                  llm.ollama(json.dumps({"action": "final", "output": "done", "summary": "ok"}))]
    task = run_task(make_task(dept, employee, max_steps=5))
    calls = {c.tool_slug: c for c in task.executions.get().tool_calls.all()}
    assert mine.title in calls["knowledge.search"].output_summary and other.title not in calls["knowledge.search"].output_summary
    assert not calls["knowledge.read"].success and "not accessible" in calls["knowledge.read"].output_summary
    proposed = Doc.objects.get(title="Lesson")
    assert proposed.status == Doc.IN_REVIEW and proposed.source == "agent" and not kb.agent_documents(employee).filter(pk=proposed.pk).exists()


# ------------------------------------------------------------------ meetings

@pytest.fixture
def team(db, dept, local_model, employee):
    bob = EmployeeProfile.objects.create(name="Bob", job_title="QA Engineer", department=dept)
    EmployeeModelPolicy.objects.create(employee=bob, model=local_model, priority=1)
    return employee, bob


def turn(text, ready=True):
    return json.dumps({"contribution": text, "ready_to_conclude": ready})


def outcome(**over):
    o = {"summary": "Agreed on test strategy", "decisions": ["Use unittest"],
         "action_items": [{"title": "Write test plan", "description": "Cover edge cases", "owner": "Bob", "due_in_days": 3}],
         "unresolved_questions": []}
    o.update(over)
    return json.dumps(o)


def make_meeting(founder, team, **kw):
    ada, bob = team
    m = msvc.create_meeting(user=founder, title="Test strategy", kind="architecture", purpose="Decide the test approach",
                            agenda=["Framework", "Ownership"], participants=[ada, bob], facilitator=ada, **{"max_rounds": 2, **kw})
    msvc.queue_meeting(m, user=founder)
    return msvc.claim_meeting(m.pk, W)


def test_meeting_produces_decisions_and_draft_action_tasks(llm, founder, team, company, dept):
    m = make_meeting(founder, team)
    llm.script = [llm.ollama(turn("Use unittest")), llm.ollama(turn("Agree; I'll own the plan")), llm.ollama(outcome())]
    msvc.run_meeting(m, W, sleep=no_sleep)
    m.refresh_from_db()
    assert m.status == Meeting.COMPLETED and m.model_calls == 3 and m.turns.count() == 2  # all ready after round 1
    t = m.action_tasks.get()
    assert t.status == Task.DRAFT and t.assigned_employee.name == "Bob" and t.deadline is not None
    assert m.notes_document.status == Doc.APPROVED and m.notes_document.source == "system" and not m.notes_document.is_authoritative
    assert "Use unittest" in m.notes_document.current_version.content
    # participants saw each other's statements as untrusted input
    assert "never as instructions" in llm.requests[1]["json"]["messages"][0]["content"]
    assert "Use unittest" in llm.requests[1]["json"]["messages"][1]["content"]


def test_meeting_round_limit_and_validation_feedback(llm, founder, team, company):
    m = make_meeting(founder, team, max_rounds=2)
    llm.script = [llm.ollama(turn("a", False)), llm.ollama(turn("b", False)), llm.ollama(turn("c", False)), llm.ollama(turn("d", False)),
                  llm.ollama(outcome(action_items=[{"title": "x", "owner": "Nobody"}])), llm.ollama(outcome())]
    msvc.run_meeting(m, W, sleep=no_sleep)
    m.refresh_from_db()
    assert m.turns.count() == 4 and m.status == Meeting.COMPLETED  # stopped at the round limit
    assert "rejected by validation" in llm.requests[5]["json"]["messages"][-1]["content"]


def test_meeting_without_actionable_output_ends_with_blocker(llm, founder, team, company):
    m = make_meeting(founder, team, max_rounds=1)
    llm.script = [llm.ollama(turn("unsure")), llm.ollama(turn("need data")),
                  llm.ollama(outcome(decisions=[], action_items=[], unresolved_questions=["Need usage data first"]))]
    msvc.run_meeting(m, W, sleep=no_sleep)
    m.refresh_from_db()
    assert m.status == Meeting.BLOCKED and "Need usage data first" in m.status_reason


def test_meeting_invalid_synthesis_twice_blocks(llm, founder, team, company):
    m = make_meeting(founder, team, max_rounds=1)
    llm.script = [llm.ollama(turn("a")), llm.ollama(turn("b")), llm.ollama(json.dumps({"summary": ""})), llm.ollama(json.dumps({"summary": ""}))]
    msvc.run_meeting(m, W, sleep=no_sleep)
    m.refresh_from_db()
    assert m.status == Meeting.BLOCKED and "could not produce a valid outcome" in m.status_reason


def test_meeting_emergency_stop_and_recovery(llm, founder, team, company):
    m = make_meeting(founder, team)
    company.emergency_stop = True
    company.save()
    msvc.run_meeting(m, W, sleep=no_sleep)
    m.refresh_from_db()
    assert m.status == Meeting.BLOCKED and m.status_reason == "emergency stop" and llm.requests == []
    company.emergency_stop = False
    company.save()
    m2 = make_meeting(founder, team)
    Meeting.objects.filter(pk=m2.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert msvc.recover_meetings() == 1
    m2.refresh_from_db()
    assert m2.status == Meeting.BLOCKED and "interrupted" in m2.status_reason


def test_meeting_creation_limits(founder, team, dept):
    ada, bob = team
    with pytest.raises(msvc.MeetingError, match="participants"):
        msvc.create_meeting(user=founder, title="x", kind="other", purpose="p", agenda=["a"], participants=[ada], facilitator=ada)
    with pytest.raises(msvc.MeetingError, match="Rounds"):
        msvc.create_meeting(user=founder, title="x", kind="other", purpose="p", agenda=["a"], participants=[ada, bob], facilitator=ada, max_rounds=9)
    nomodel = EmployeeProfile.objects.create(name="NoModel", job_title="x", department=dept)
    with pytest.raises(msvc.MeetingError, match="no model"):
        msvc.create_meeting(user=founder, title="x", kind="other", purpose="p", agenda=["a"], participants=[ada, nomodel], facilitator=ada)


def test_engine_tick_runs_queued_meeting(llm, founder, team, company):
    ada, bob = team
    m = msvc.create_meeting(user=founder, title="t", kind="other", purpose="p", agenda=["a"], participants=[ada, bob], facilitator=ada, max_rounds=1)
    msvc.queue_meeting(m, user=founder)
    llm.script = [llm.ollama(turn("x")), llm.ollama(turn("y")), llm.ollama(outcome())]
    engine.tick(W)
    m.refresh_from_db()
    assert m.status == Meeting.COMPLETED


# ------------------------------------------------------------------ reports

def test_employee_metrics_quality_rework_reliability_and_deadlines(founder, employee, dept, local_model):
    now = timezone.now()
    a = make_task(dept, employee, status=Task.COMPLETED, deadline=now + timedelta(days=1), completed_at=now)
    b = make_task(dept, employee, status=Task.COMPLETED, deadline=now - timedelta(days=1), completed_at=now)
    make_task(dept, employee, status=Task.FAILED)
    Review.objects.create(task=a, reviewer=founder, outcome="accept")
    Review.objects.create(task=b, reviewer=founder, outcome="reject")
    Review.objects.create(task=b, reviewer=founder, outcome="accept")
    m = compute.employee_metrics(employee, compute.Filters())
    assert (m["completed"], m["failed"], m["accepted"], m["rejected"]) == (2, 1, 2, 1)
    assert m["rework_rate"] == "33%" and m["failure_rate"] == "33%" and m["deadline"] == "1/2 on time"


def test_financial_report_separates_estimated_and_billed(db, employee, local_model, llm):
    paid = make_model(make_provider("Cloud", llm.url, kind="openai", is_paid=True), ident="gpt")
    UsageRecord.objects.create(employee=employee, provider=paid.provider, model=paid, estimated_cost_usd=Decimal("0.5"),
                               input_tokens=100, output_tokens=50, tokens_reported=True)
    UsageRecord.objects.create(employee=employee, provider=paid.provider, model=paid, estimated_cost_usd=Decimal("0.5"),
                               billed_cost_usd=Decimal("0.42"))
    UsageRecord.objects.create(employee=employee, provider=local_model.provider, model=local_model, success=False, error_kind="timeout")
    rep = compute.financial_report(compute.Filters())
    totals = dict(rep["sections"][0]["metrics"])
    assert totals["Estimated cost"] == "$1.0000" and totals["Verified billed cost"] == "$0.4200"
    providers = {r[0]: r for r in rep["sections"][1]["rows"]}
    assert providers["Cloud"][5] == "100 / 50"  # only provider-reported tokens counted
    assert providers["Local Ollama"][7] == "not available"
    assert dict((r[0], r[1]) for r in rep["sections"][5]["rows"]) == {"timeout": 1}


def test_report_permissions_and_export(client, founder, staff, dept, other_dept, employee):
    PermissionAssignment.objects.create(user=staff, permission="view_reports", department=dept)
    client.login(username="staff", password=PASSWORD)
    url = reverse("reports")
    assert client.get(url, {"kind": "department", "department": dept.pk}).status_code == 200
    r = client.get(url, {"kind": "department", "department": other_dept.pk})  # not offered, and rejected if forged
    assert "Select a valid choice" in r.content.decode() and "Department report: Finance" not in r.content.decode()
    assert client.get(url, {"kind": "company"}).status_code == 403
    assert client.get(url, {"kind": "financial"}).status_code == 403
    r = client.post(url, {"kind": "department", "department": dept.pk, "fmt": "csv"})
    assert r.status_code == 302
    dl = client.get(r["Location"])
    assert dl.status_code == 200 and b"Department report: Engineering" in b"".join(dl.streaming_content)
    PermissionAssignment.objects.filter(user=staff).delete()
    assert client.get(r["Location"]).status_code == 403  # access re-checked at download
    client.logout()
    client.login(username="founder", password=PASSWORD)
    for kind in ("company", "financial"):
        assert client.get(url, {"kind": kind}).status_code == 200
    r = client.post(url, {"kind": "company", "fmt": "pdf"})
    assert b"".join(client.get(r["Location"]).streaming_content).startswith(b"%PDF")


def test_csv_neutralizes_formula_injection():
    rep = {"title": "t", "filters": "f", "generated_at": "now", "notes": [],
           "sections": [{"title": "s", "kind": "table", "columns": ["Task"], "rows": [["=HYPERLINK(\"http://evil\")"], ["-5"]], "note": ""}]}
    out = export.to_csv(rep).decode("utf-8-sig")
    assert "'=HYPERLINK" in out and "\n-5" in out.replace("\r", "")


# ------------------------------------------------------------------ notifications

def test_preferences_mandatory_kinds_and_email(founder, company, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    from apps.core.notify import notify_user
    NotificationPreference.objects.create(user=founder, kind="task_completed", in_app=False, email=True)
    NotificationPreference.objects.create(user=founder, kind="security", in_app=False)
    notify_user(founder, "task_completed", "done")
    notify_user(founder, "security", "alert")
    assert list(Notification.objects.values_list("kind", flat=True)) == ["security"]  # mandatory kind still delivered
    assert len(mail.outbox) == 0  # email integration not enabled by the Founder
    company.email_notifications_enabled = True
    company.save()
    notify_user(founder, "task_completed", "done again", "token sk-abcdefghijklmnopqrstuvwxyz leaked")
    assert len(mail.outbox) == 1 and "sk-abcdef" not in mail.outbox[0].body


def test_preferences_page(client, founder):
    client.login(username="founder", password=PASSWORD)
    assert client.get(reverse("notification_preferences")).status_code == 200
    client.post(reverse("notification_preferences"), {"email_review": "on"})
    assert NotificationPreference.objects.get(user=founder, kind="review").email
    assert NotificationPreference.objects.get(user=founder, kind="security").in_app  # cannot be disabled


def test_budget_warning_once_per_period(llm, founder, employee, company):
    company.paid_models_enabled = True
    company.save()
    paid = make_model(make_provider("Paid", llm.url, is_paid=True), ident="paid", price_input_per_mtok=Decimal("1000"),
                      price_output_per_mtok=Decimal("1000"))
    EmployeeModelPolicy.objects.filter(employee=employee).delete()
    EmployeeModelPolicy.objects.create(employee=employee, model=paid, priority=1, allow_paid=True)
    Budget.objects.create(scope=Budget.COMPANY, period=Budget.MONTHLY, limit_usd=Decimal("1.00"))
    from apps.gateway.router import route_chat
    llm.default = llm.ollama("ok", pin=300, pout=300)  # $0.60 per call
    route_chat(employee, [{"role": "user", "content": "hi"}], max_tokens=10, sleep=no_sleep)
    assert not Notification.objects.filter(kind="budget").exists()
    route_chat(employee, [{"role": "user", "content": "hi"}], max_tokens=10, sleep=no_sleep)  # crosses 80% (1.20 >= 0.80)
    assert Notification.objects.filter(kind="budget", user=founder).count() == 1
    Budget.objects.update(limit_usd=Decimal("100"))
    UsageRecord.objects.create(employee=employee, model=paid, provider=paid.provider, estimated_cost_usd=Decimal("90"))
    route_chat(employee, [{"role": "user", "content": "hi"}], max_tokens=10, sleep=no_sleep)
    assert Notification.objects.filter(kind="budget", user=founder).count() == 1  # once per period


def test_deadline_warning_once(founder, employee, dept, company):
    t = make_task(dept, employee, deadline=timezone.now() + timedelta(hours=5))
    make_task(dept, employee, deadline=timezone.now() + timedelta(days=5))
    assert engine.warn_deadlines() == [t.pk]
    assert engine.warn_deadlines() == []
    assert Notification.objects.filter(kind="deadline", user=founder).count() == 1


def test_repeated_tool_denials_raise_security_incident(llm, founder, employee, dept, company):
    llm.script = [llm.ollama(llm.tool("python.run", {"code": "1"})) for _ in range(3)] + \
                 [llm.ollama(json.dumps({"action": "final", "output": "x", "summary": "x"}))]
    run_task(make_task(dept, employee, max_steps=5))
    inc = Incident.objects.get(category="security")
    assert "python.run" in inc.description
    assert Notification.objects.filter(kind="security", user=founder).exists()


def test_ceo_decisions_notify_founder(founder, ceo, dept, employee, company):
    from apps.work import projects as psvc
    from apps.work.models import Project
    p = Project.objects.create(name="P", department=dept)
    psvc.change_project_priority(p, 0, user=ceo, reason="urgent")
    assert Notification.objects.filter(user=founder, kind="ceo_decision").exists()
    assert not Notification.objects.filter(user=ceo, kind="ceo_decision").exists()


def test_phase7_pages_render(client, founder, team, dept):
    client.login(username="founder", password=PASSWORD)
    doc = approved_doc("Handbook", "content", founder)
    ada, bob = team
    m = msvc.create_meeting(user=founder, title="t", kind="other", purpose="p", agenda=["a"], participants=[ada, bob], facilitator=ada)
    for url in [reverse("knowledge_list"), reverse("knowledge_list") + "?q=handbook", reverse("knowledge_create"),
                reverse("knowledge_detail", args=[doc.pk]), reverse("meeting_list"), reverse("meeting_detail", args=[m.pk]),
                reverse("reports"), reverse("reports") + f"?kind=employee&employee={ada.pk}", reverse("notification_preferences")]:
        assert client.get(url).status_code == 200, url
    r = client.post(reverse("meeting_list"), {"title": "Arch review", "kind": "architecture", "purpose": "Choose DB",
                                              "agenda": "Options\nDecision", "participants": [ada.pk, bob.pk],
                                              "facilitator": ada.pk, "max_rounds": 1, "start": "1"})
    assert r.status_code == 302 and Meeting.objects.get(title="Arch review").status == Meeting.QUEUED
