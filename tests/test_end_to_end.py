"""Section 26 vertical slice, driven through the real HTTP views, the real engine and the real
Ollama adapter (talking to the scripted test server over HTTP)."""
import json

from django.core.management import call_command
from django.urls import reverse

from apps.core.audit import verify_chain
from apps.core.models import AuditEvent, Department, EmployeeProfile, RoleTemplate, User
from apps.gateway.models import AIModel, ModelProvider, UsageRecord
from apps.orchestration import engine
from apps.tools.models import ToolDefinition
from apps.work.models import Task, ToolCall

from .conftest import PASSWORD


def test_first_end_to_end_demonstration(client, db, llm, company):
    call_command("seed_vcoos")  # role templates + tool registry
    founder = User.objects.create_user("founder", password=PASSWORD, account_type=User.FOUNDER)
    ceo = User.objects.create_user("ceo", password=PASSWORD, account_type=User.CEO)

    # 1. Founder logs in
    r = client.post(reverse("login"), {"username": "founder", "password": PASSWORD})
    assert r.status_code == 302

    # 2. Founder creates a department
    r = client.post(reverse("department_list"), {"name": "Software Engineering", "description": "Builds apps", "color": "#3b82f6",
                                                 "office_x": 0, "office_z": 0, "office_floor": 0, "is_active": "on"})
    dept = Department.objects.get(name="Software Engineering")
    assert r.status_code == 302 and dept.slug == "software-engineering"

    # 3. Founder configures an approved local model (Ollama provider -> health check -> model)
    client.post(reverse("provider_list"), {"name": "Local Ollama", "kind": "ollama", "base_url": llm.url, "is_enabled": "on",
                                           "is_local": "on", "timeout_seconds": 30})
    provider = ModelProvider.objects.get(name="Local Ollama")
    client.post(reverse("provider_detail", args=[provider.pk]), {"action": "health"})
    provider.refresh_from_db()
    assert provider.health_status == "healthy"
    client.post(reverse("provider_detail", args=[provider.pk]), {
        "action": "add_model", "provider": provider.pk, "model_identifier": "fake-model:latest", "display_name": "Local test model",
        "max_output_tokens": 512, "supports_json": "on", "price_input_per_mtok": "0", "price_output_per_mtok": "0",
        "is_enabled": "on", "is_approved": "on"})
    model = AIModel.objects.get(model_identifier="fake-model:latest")
    assert model.is_approved

    # 4. Founder creates an AI employee from a role template
    tpl = RoleTemplate.objects.get(name="Python Developer")
    r = client.post(reverse("employee_create"), {"name": "Ada", "department": dept.pk, "role_template": tpl.pk, "job_title": "",
                                                 "avatar_color": "#22c55e", "max_concurrent_tasks": 1, "skills_text": ""})
    emp = EmployeeProfile.objects.get(name="Ada")
    assert emp.job_title == "Python Developer" and "python" in emp.skills
    client.post(reverse("employee_detail", args=[emp.pk]), {"action": "add_model", "model": model.pk})
    write_tool = ToolDefinition.objects.get(slug="workspace.write_file")
    client.post(reverse("employee_detail", args=[emp.pk]), {"action": "grant_tool", "tool": write_tool.pk})
    assert emp.model_policies.get().model == model

    # 5. Founder assigns a task (CEO is the reviewer)
    r = client.post(reverse("task_create"), {
        "title": "Implement add()", "description": "Write add(a, b) in add.py", "acceptance_criteria": "add(2, 3) == 5",
        "department": dept.pk, "assigned_employee": emp.pk, "priority": 2, "requires_review": "on", "reviewer": ceo.pk,
        "is_idempotent": "on", "max_steps": 5, "max_retries": 1, "max_runtime_seconds": 120, "submit": "queue"})
    task = Task.objects.get(title="Implement add()")
    assert task.status == Task.QUEUED, task.status_reason

    # 6-9. The engine executes; the employee uses its permitted tool and is refused an unpermitted one.
    llm.script = [
        llm.ollama(llm.tool("workspace.write_file", {"path": "add.py", "content": "def add(a, b):\n    return a + b\n"})),
        llm.ollama(llm.tool("python.run", {"code": "print(1)"})),
        llm.ollama(llm.final("Created add.py with add(a, b) returning a + b.", summary="add() implemented")),
    ]
    assert engine.tick("e2e-worker") == [task.pk]
    task.refresh_from_db()
    assert task.status == Task.UNDER_REVIEW
    execution = task.executions.get()
    assert execution.model_used == model
    calls = {c.tool_slug: c for c in ToolCall.objects.filter(execution=execution)}
    assert calls["workspace.write_file"].allowed and calls["workspace.write_file"].success
    assert not calls["python.run"].allowed
    assert [s["action"] for s in execution.steps] == ["tool", "tool", "final"]
    assert UsageRecord.objects.filter(task=task, success=True).count() == 3

    # 10. Dashboard and office API show the real status
    state = client.get(reverse("api_office_state")).json()
    ada = next(e for d in state["departments"] for e in d["employees"] if e["id"] == emp.pk)
    assert ada["state"] == "under_review" and ada["task"]["code"] == task.code
    detail = client.get(reverse("api_employee", args=[emp.pk])).json()
    assert detail["last_model_used"] == "Local test model" and "workspace.write_file" in detail["tools"]
    assert client.get(reverse("dashboard")).status_code == 200
    assert client.get(reverse("office")).status_code == 200

    # 11-12. An authorized reviewer (the CEO) accepts
    client.post(reverse("logout"))
    client.post(reverse("login"), {"username": "ceo", "password": PASSWORD})
    r = client.post(reverse("task_action", args=[task.pk]), {"action": "review", "outcome": "accept", "comments": "Meets criteria."})
    task.refresh_from_db()

    # 13. Final status and audit events recorded
    assert task.status == Task.COMPLETED
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    for a in ("department.created", "provider.created", "model.created", "employee.created", "tool.granted",
              "task.created", "task.status", "tool.denied", "task.reviewed"):
        assert a in actions, a
    assert verify_chain() == (True, None)

    # 14. Founder inspects task history and usage
    client.post(reverse("logout"))
    client.post(reverse("login"), {"username": "founder", "password": PASSWORD})
    page = client.get(reverse("task_detail", args=[task.pk])).content.decode()
    for text in ("Completed", "Local test model", "workspace.write_file", "permission denied", "Meets criteria.", "under_review"):
        assert text in page, text
    assert list(task.status_history.values_list("to_status", flat=True)) == ["queued", "running", "under_review", "completed"]
