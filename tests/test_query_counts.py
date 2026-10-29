"""Query-count scaling: key pages must not issue queries per row (N+1)."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.core.models import Department, EmployeeProfile
from apps.gateway.models import EmployeeModelPolicy
from apps.tools.models import ToolPermission
from apps.work.models import Task

from .conftest import PASSWORD

PAGES = ["dashboard", "employee_list", "task_board", "api_office_state", "department_list", "monitor", "project_list",
         "proposal_board", "knowledge_list", "approvals", "audit_log", "provider_list", "tool_registry"]


def populate(n, dept, model, tools):
    for i in range(n):
        e = EmployeeProfile.objects.create(name=f"E{i}-{EmployeeProfile.objects.count()}", job_title="Dev", department=dept)
        EmployeeModelPolicy.objects.create(employee=e, model=model, priority=1)
        ToolPermission.objects.create(tool=tools["workspace.read_file"], scope="employee", employee=e)
        for status in (Task.QUEUED, Task.RUNNING, Task.UNDER_REVIEW):
            Task.objects.create(title=f"t{i}{status}", description="x", department=dept, assigned_employee=e, status=status)


def count(client, name):
    with CaptureQueriesContext(connection) as ctx:
        r = client.get(reverse(name))
    assert r.status_code == 200, name
    return len(ctx.captured_queries)


def test_query_counts_do_not_scale_with_rows(client, founder, dept, local_model, tools, company):
    client.login(username="founder", password=PASSWORD)
    populate(3, dept, local_model, tools)
    small = {p: count(client, p) for p in PAGES}
    populate(30, dept, local_model, tools)
    large = {p: count(client, p) for p in PAGES}
    growth = {p: large[p] - small[p] for p in PAGES}
    print("\nqueries small:", small, "\nqueries large:", large)
    assert all(g <= 3 for g in growth.values()), growth
