"""Builds the office view model from real backend state only (no simulated activity)."""
from django.db.models import Prefetch

from apps.core.models import Department, EmployeeProfile
from apps.core.permissions import can, scope_queryset
from apps.gateway.models import EmployeeModelPolicy
from apps.tools.registry import permitted_tools
from apps.work.models import Task

T = Task
# Precedence when an employee has several open tasks.
VISUAL_ORDER = [
    (T.RUNNING, "working"), (T.AWAITING_APPROVAL, "awaiting_approval"), (T.WAITING, "waiting"),
    (T.BLOCKED, "blocked"), (T.UNDER_REVIEW, "under_review"), (T.PAUSED, "paused"), (T.FAILED, "failed"),
]
OPEN_STATUSES = [s for s, _ in VISUAL_ORDER] + [T.QUEUED]


def employee_visual_state(emp, open_tasks):
    if emp.status == EmployeeProfile.PAUSED or emp.department.is_paused:
        return "paused", None
    if emp.status in (EmployeeProfile.SUSPENDED, EmployeeProfile.OFFLINE) or not emp.department.is_active:
        return "offline", None
    for status, visual in VISUAL_ORDER:
        for t in open_tasks:
            if t.status == status:
                return visual, t
    queued = [t for t in open_tasks if t.status == T.QUEUED]
    return ("idle", queued[0]) if queued else ("idle", None)


def serialize_employee(emp, open_tasks, detail=False):
    visual, task = employee_visual_state(emp, open_tasks)
    policies = sorted(emp.model_policies.all(), key=lambda p: p.priority)
    data = {
        "id": emp.pk, "employee_id": emp.employee_id, "name": emp.name, "job_title": emp.job_title,
        "department_id": emp.department_id, "color": emp.avatar_color, "state": visual, "profile_status": emp.status,
        "primary_model": str(policies[0].model.display_name) if policies else None,
        "task": None,
    }
    if task is not None:
        data["task"] = {"id": task.pk, "code": task.code, "title": task.title, "status": task.status,
                        "status_label": task.get_status_display(), "progress_note": task.progress_note,
                        "project": task.project.name if task.project_id else None, "updated_at": task.updated_at.isoformat()}
    if detail:
        last_exec = task.executions.select_related("model_used").first() if task else None
        data.update({
            "role_description": emp.role_description, "skills": emp.skills,
            "models": [{"priority": p.priority, "name": p.model.display_name, "provider": p.model.provider.name} for p in policies],
            "last_model_used": last_exec.model_used.display_name if last_exec and last_exec.model_used else None,
            "tools": [t.slug for t in permitted_tools(emp, task)] if task else
                     [tp.tool.slug for tp in emp.tool_permissions.filter(allow=True).select_related("tool")],
            "recent_steps": (last_exec.steps[-5:] if last_exec else []),
        })
    return data


def office_state(user):
    employees = scope_queryset(user, "view_employees", EmployeeProfile.objects.all(), project_field=None)
    employees = employees.select_related("department").prefetch_related(
        Prefetch("model_policies", queryset=EmployeeModelPolicy.objects.select_related("model__provider")),
        Prefetch("tasks", queryset=Task.objects.filter(status__in=OPEN_STATUSES).select_related("project").order_by("priority", "-updated_at"), to_attr="open_tasks"),
    )
    depts = scope_queryset(user, "view_department", Department.objects.filter(is_active=True), dept_field="pk", project_field=None)
    by_dept = {}
    for emp in employees:
        by_dept.setdefault(emp.department_id, []).append(serialize_employee(emp, emp.open_tasks))
    out = []
    for d in depts:
        emps = by_dept.get(d.pk, [])
        out.append({
            "id": d.pk, "name": d.name, "color": d.color, "x": d.office_x, "z": d.office_z, "floor": d.office_floor,
            "paused": d.is_paused, "employees": emps,
            "counts": {s: sum(1 for e in emps if e["state"] == s) for s in {e["state"] for e in emps}},
        })
    return {"departments": out, "can_control": can(user, "control_employees")}
