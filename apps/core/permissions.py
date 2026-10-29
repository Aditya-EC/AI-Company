"""Central server-side authorization. Default deny.

Founder: everything. CEO: operational permissions only (never Founder-exclusive ones).
Staff: only what PermissionAssignment grants, scoped by department and/or project.
"""
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .models import PermissionAssignment

FOUNDER_ONLY = {
    "manage_settings",
    "manage_models",
    "manage_tools",
    "manage_permissions",
    "manage_departments",
    "manage_employees",
    "manage_budgets",
    "emergency_stop",
    "appoint_ceo",
}

CEO_ALLOWED = {
    "view_company",
    "view_department",
    "view_employees",
    "control_employees",  # pause / resume
    "view_tasks",
    "manage_tasks",
    "review_tasks",
    "view_reports",
    "view_monitor",
    "view_models",
    "view_audit",
}

STAFF_GRANTABLE = {code for code, _ in PermissionAssignment.PERMISSIONS}


def _scope_of(obj):
    """Return (department_id, project_id) that an object belongs to."""
    if obj is None:
        return None, None
    label = obj._meta.label_lower
    if label == "core.department":
        return obj.pk, None
    if label == "core.employeeprofile":
        return obj.department_id, None
    if label == "work.project":
        return obj.department_id, obj.pk
    if label in ("work.task",):
        return obj.department_id, obj.project_id
    return getattr(obj, "department_id", None), getattr(obj, "project_id", None)


def can(user, perm, obj=None):
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_founder:
        return True
    if perm in FOUNDER_ONLY:
        return False
    if user.is_ceo:
        return perm in CEO_ALLOWED
    if perm not in STAFF_GRANTABLE:
        return False
    grants = user.permission_assignments.filter(permission=perm)
    if obj is None:
        return grants.exists()
    dept_id, project_id = _scope_of(obj)
    scope = Q(department__isnull=True, project__isnull=True)
    if dept_id:
        scope |= Q(department_id=dept_id, project__isnull=True)
    if project_id:
        scope |= Q(project_id=project_id)
    return grants.filter(scope).exists()


def require(user, perm, obj=None):
    if not can(user, perm, obj):
        raise PermissionDenied(f"Missing permission: {perm}")


def scope_queryset(user, perm, qs, dept_field="department", project_field="project"):
    """Restrict a queryset to rows the user may access with `perm`."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return qs.none()
    if user.is_founder or (user.is_ceo and perm in CEO_ALLOWED):
        return qs
    if user.is_ceo or perm not in STAFF_GRANTABLE:
        return qs.none()
    grants = list(user.permission_assignments.filter(permission=perm).values("department_id", "project_id"))
    if any(g["department_id"] is None and g["project_id"] is None for g in grants):
        return qs
    cond = Q(pk__in=[])
    dept_ids = [g["department_id"] for g in grants if g["department_id"] and not g["project_id"]]
    proj_ids = [g["project_id"] for g in grants if g["project_id"]]
    if dept_ids and dept_field:
        cond |= Q(**{f"{dept_field}__in": dept_ids})
    if proj_ids and project_field:
        cond |= Q(**{f"{project_field}__in": proj_ids})
    return qs.filter(cond)
