"""Office HQ data contract: every number the 3D office shows comes from real records and respects scope."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.core.models import PermissionAssignment
from apps.orchestration import engine
from apps.work.models import Task

from .conftest import PASSWORD, grant, make_task


def test_team_stats_and_hub_reflect_real_records(client, founder, dept, other_dept, employee, tools, company):
    now = timezone.now()
    make_task(dept, employee, status=Task.RUNNING)
    make_task(dept, employee, status=Task.QUEUED)
    make_task(dept, employee, status=Task.UNDER_REVIEW)
    make_task(dept, employee, status=Task.BLOCKED)
    make_task(dept, employee, status=Task.COMPLETED, started_at=now - timedelta(minutes=30), completed_at=now)
    make_task(dept, employee, status=Task.COMPLETED, started_at=now - timedelta(minutes=10), completed_at=now)
    make_task(dept, employee, status=Task.COMPLETED, started_at=now - timedelta(days=3), completed_at=now - timedelta(days=2))
    grant(tools["workspace.read_file"], department=dept)
    grant(tools["python.run"], employee=employee)
    engine.heartbeat("w1")
    client.login(username="founder", password=PASSWORD)
    data = client.get(reverse("api_office_state")).json()
    d = next(x for x in data["departments"] if x["id"] == dept.pk)
    s = d["stats"]
    assert (s["running"], s["queued"], s["review"], s["blocked"], s["completed_today"]) == (1, 1, 1, 1, 2)
    assert s["avg_turnaround_min"] == 20.0  # (30 + 10) / 2, today's completions only
    assert sorted(s["tools"]) == ["python.run", "workspace.read_file"]
    other = next(x for x in data["departments"] if x["id"] == other_dept.pk)
    assert other["stats"]["running"] == 0 and other["stats"]["avg_turnaround_min"] is None
    hub = data["hub"]
    assert hub["running"] == 1 and hub["queued"] == 1 and hub["workers"] == 1 and hub["agents"] == 1


def test_hub_totals_only_cover_visible_departments(client, staff, dept, other_dept, employee, company):
    make_task(other_dept, None, status=Task.RUNNING)
    make_task(dept, employee, status=Task.RUNNING)
    for perm in ("view_employees", "view_department"):
        PermissionAssignment.objects.create(user=staff, permission=perm, department=dept)
    client.login(username="staff", password=PASSWORD)
    data = client.get(reverse("api_office_state")).json()
    assert [d["id"] for d in data["departments"]] == [dept.pk]
    assert data["hub"]["running"] == 1  # the other department's running task is not revealed
