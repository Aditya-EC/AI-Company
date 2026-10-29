"""Every page renders for authorized roles; report/budget calculations are correct."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.core import budget
from apps.core.models import Budget
from apps.gateway.models import UsageRecord
from apps.work import services
from apps.work.models import Review, Task

from .conftest import PASSWORD, make_task


@pytest.fixture
def populated(db, founder, ceo, employee, dept, local_model, company):
    call_command("seed_vcoos")
    t = make_task(dept, employee, created_by=founder)
    services.queue_task(t)
    Budget.objects.create(scope=Budget.COMPANY, period=Budget.MONTHLY, limit_usd=Decimal("10"))
    return t


FOUNDER_PAGES = ["dashboard", "office", "monitor", "department_list", "employee_list", "employee_create", "role_templates",
                 "provider_list", "tool_registry", "task_board", "task_create", "approvals", "audit_log", "company_settings",
                 "users", "budgets", "notifications", "password_change"]


def test_founder_pages_render(client, populated, employee, dept, local_model):
    client.login(username="founder", password=PASSWORD)
    for name in FOUNDER_PAGES:
        r = client.get(reverse(name))
        assert r.status_code == 200, name
    for name, pk in [("department_detail", dept.pk), ("employee_detail", employee.pk), ("task_detail", populated.pk),
                     ("provider_detail", local_model.provider_id), ("model_detail", local_model.pk)]:
        assert client.get(reverse(name, args=[pk])).status_code == 200, name
    assert client.get(reverse("office") + "?mode=2d").status_code == 200
    assert client.get(reverse("audit_log") + "?verify=1").status_code == 200


def test_ceo_pages_render_and_founder_pages_forbidden(client, populated):
    client.login(username="ceo", password=PASSWORD)
    for name in ["dashboard", "office", "monitor", "employee_list", "task_board", "approvals", "audit_log", "provider_list", "budgets"]:
        assert client.get(reverse(name)).status_code == 200, name
    for name in ["company_settings", "users", "employee_create"]:
        assert client.get(reverse(name)).status_code == 403, name


def test_model_priority_reorder(client, populated, employee, llm):
    from .conftest import make_model, make_provider
    from apps.gateway.models import EmployeeModelPolicy
    second = make_model(make_provider("B", llm.url), ident="b")
    EmployeeModelPolicy.objects.create(employee=employee, model=second, priority=2)
    client.login(username="founder", password=PASSWORD)
    p2 = employee.model_policies.get(model=second)
    client.post(reverse("employee_detail", args=[employee.pk]), {"action": "model_up", "policy": p2.pk})
    assert list(employee.model_policies.order_by("priority").values_list("model__model_identifier", flat=True)) == ["b", "fake-model:latest"]


def test_budget_spent_prefers_billed_cost(db, employee, local_model):
    b = Budget.objects.create(scope=Budget.EMPLOYEE, employee=employee, period=Budget.DAILY, limit_usd=Decimal("5"))
    UsageRecord.objects.create(employee=employee, model=local_model, estimated_cost_usd=Decimal("1.0"))
    UsageRecord.objects.create(employee=employee, model=local_model, estimated_cost_usd=Decimal("1.0"), billed_cost_usd=Decimal("0.5"))
    old = UsageRecord.objects.create(employee=employee, model=local_model, estimated_cost_usd=Decimal("9"))
    UsageRecord.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=2))  # outside the daily period
    assert budget.spent(b) == Decimal("1.5")


def test_dashboard_completion_rate_and_employee_rework(client, populated, founder, employee, dept):
    done = make_task(dept, employee, status=Task.COMPLETED)
    make_task(dept, employee, status=Task.FAILED)
    rejected = make_task(dept, employee, status=Task.REVISION)
    Review.objects.create(task=done, reviewer=founder, outcome="accept")
    Review.objects.create(task=rejected, reviewer=founder, outcome="reject")
    client.login(username="founder", password=PASSWORD)
    r = client.get(reverse("dashboard"))
    assert r.context["completion_rate"] == 50 and r.context["done_30d"] == 1
    m = client.get(reverse("employee_detail", args=[employee.pk])).context["metrics"]
    assert (m["completed"], m["failed"], m["accepted"], m["rejected"]) == (1, 1, 1, 1)
