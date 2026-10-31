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
    depts = list(scope_queryset(user, "view_department", Department.objects.filter(is_active=True), dept_field="pk",
                                project_field=None).select_related("manager"))
    by_dept = {}
    for emp in employees:
        by_dept.setdefault(emp.department_id, []).append(serialize_employee(emp, emp.open_tasks))
    stats = team_stats([d.pk for d in depts])
    out = []
    for d in depts:
        emps = by_dept.get(d.pk, [])
        out.append({
            "id": d.pk, "name": d.name, "color": d.color, "x": d.office_x, "z": d.office_z, "floor": d.office_floor,
            "paused": d.is_paused, "employees": emps, "manager": d.manager.name if d.manager_id else None,
            "counts": {s: sum(1 for e in emps if e["state"] == s) for s in {e["state"] for e in emps}},
            "stats": stats.get(d.pk, EMPTY_STATS),
        })
    return {"departments": out, "can_control": can(user, "control_employees"), "hub": hub_summary(user, out)}


EMPTY_STATS = {"running": 0, "queued": 0, "blocked": 0, "review": 0, "completed_today": 0, "avg_turnaround_min": None, "tools": []}


def team_stats(dept_ids):
    """Real per-department figures for the office panels (a fixed number of queries)."""
    from django.db.models import Count, Q
    from django.utils import timezone

    from apps.tools.models import ToolPermission

    today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = Task.objects.filter(department_id__in=dept_ids).values("department_id").annotate(
        running=Count("id", filter=Q(status=T.RUNNING)), queued=Count("id", filter=Q(status=T.QUEUED)),
        blocked=Count("id", filter=Q(status__in=[T.BLOCKED, T.WAITING, T.AWAITING_APPROVAL, T.PAUSED, T.FAILED])),
        review=Count("id", filter=Q(status=T.UNDER_REVIEW)),
        completed_today=Count("id", filter=Q(status=T.COMPLETED, completed_at__gte=today)))
    out = {r["department_id"]: {**EMPTY_STATS, **{k: v for k, v in r.items() if k != "department_id"}, "tools": []} for r in rows}
    durations = {}
    for dept_id, started, done in Task.objects.filter(department_id__in=dept_ids, status=T.COMPLETED, completed_at__gte=today,
                                                      started_at__isnull=False).values_list("department_id", "started_at", "completed_at"):
        durations.setdefault(dept_id, []).append((done - started).total_seconds() / 60)
    for dept_id, mins in durations.items():
        out.setdefault(dept_id, {**EMPTY_STATS, "tools": []})["avg_turnaround_min"] = round(sum(mins) / len(mins), 1)
    grants = ToolPermission.objects.filter(allow=True, tool__is_enabled=True).filter(
        Q(scope="department", department_id__in=dept_ids) | Q(scope="employee", employee__department_id__in=dept_ids))
    for scope, dept_id, emp_dept, slug in grants.values_list("scope", "department_id", "employee__department_id", "tool__slug"):
        target = dept_id if scope == "department" else emp_dept
        entry = out.setdefault(target, {**EMPTY_STATS, "tools": []})
        if slug not in entry["tools"]:
            entry["tools"].append(slug)
    return out


def hub_summary(user, departments):
    """The central hub represents the shared orchestration engine: totals over the departments the viewer may see."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.gateway.models import UsageRecord
    from apps.orchestration.models import WorkerHeartbeat

    totals = {k: sum(d["stats"][k] for d in departments) for k in ("running", "queued", "blocked", "review", "completed_today")}
    agents = sum(len(d["employees"]) for d in departments)
    models = list(UsageRecord.objects.filter(success=True, created_at__gte=timezone.now() - timedelta(hours=24))
                  .values_list("model__display_name", flat=True).distinct()[:6])
    try:
        from apps.knowledge.services import visible_documents

        knowledge = visible_documents(user).filter(status="approved").count()
    except Exception:
        knowledge = None
    return {**totals, "agents": agents, "workers": WorkerHeartbeat.alive().count(), "models_24h": [m for m in models if m],
            "knowledge_docs": knowledge}
