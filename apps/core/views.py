from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.gateway.models import AIModel, EmployeeModelPolicy, ModelProvider, UsageRecord
from apps.tools.models import ToolDefinition, ToolPermission
from apps.work.models import ApprovalRequest, Task

from . import audit, budget, ratelimit
from .forms import (BudgetForm, CompanySettingsForm, DepartmentForm, EmployeeForm, PermissionAssignmentForm, RoleTemplateForm,
                    UserCreateForm)
from .models import AuditEvent, Budget, CompanySettings, Department, EmployeeProfile, Incident, PermissionAssignment, RoleTemplate, User
from .permissions import can, require, scope_queryset


# ------------------------------------------------------------------ auth

class RateLimitedLoginView(auth_views.LoginView):
    template_name = "auth/login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        ip, username = request.META.get("REMOTE_ADDR", ""), request.POST.get("username", "")
        if ratelimit.is_limited(ip, username):
            audit.record("auth.rate_limited", details={"username": username[:150]}, request=request)
            form = self.get_form()
            form.errors.clear()
            messages.error(request, "Too many failed attempts. Try again later.")
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        ratelimit.register_failure(self.request.META.get("REMOTE_ADDR", ""), self.request.POST.get("username", ""))
        audit.record("auth.login_failed", details={"username": self.request.POST.get("username", "")[:150]}, request=self.request)
        return super().form_invalid(form)

    def form_valid(self, form):
        ratelimit.reset(self.request.META.get("REMOTE_ADDR", ""), form.get_user().username)
        response = super().form_valid(form)
        audit.record("auth.login", user=form.get_user(), request=self.request)
        return response


def healthz_ready(request):
    from . import health

    if not health.authorized(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    ok, results = health.readiness()
    return JsonResponse({"status": "ready" if ok else "not_ready", "checks": results}, status=200 if ok else 503)


def metrics(request):
    from django.http import HttpResponse

    from . import health

    if not health.authorized(request):
        return HttpResponse("unauthorized\n", status=401, content_type="text/plain")
    return HttpResponse(health.metrics_text(), content_type="text/plain; version=0.0.4")


class RateLimitedPasswordResetView(auth_views.PasswordResetView):
    """Limits reset emails per client IP and per address to prevent abuse as a mail cannon."""

    template_name = "auth/password_reset.html"
    email_template_name = "auth/password_reset_email.txt"

    def post(self, request, *args, **kwargs):
        ip, email = request.META.get("REMOTE_ADDR", ""), request.POST.get("email", "")[:254]
        key_user = f"reset:{email.lower()}"
        if ratelimit.is_limited(ip, key_user):
            audit.record("auth.password_reset_rate_limited", details={"email": email}, request=request)
            return redirect("password_reset_done")  # same response as success: no account enumeration
        ratelimit.register_failure(ip, key_user)
        return super().post(request, *args, **kwargs)


def healthz(request):
    from django.db import connection

    try:
        connection.ensure_connection()
        db = "ok"
    except Exception:
        db = "error"
    return JsonResponse({"status": "ok" if db == "ok" else "degraded", "database": db}, status=200 if db == "ok" else 503)


# ------------------------------------------------------------------ dashboard

def _employee_state_counts(user):
    from apps.office.state import office_state

    counts = {}
    for d in office_state(user)["departments"]:
        for e in d["employees"]:
            counts[e["state"]] = counts.get(e["state"], 0) + 1
    return counts


def _workers_alive():
    from apps.orchestration.models import WorkerHeartbeat

    return WorkerHeartbeat.alive().count()


def dashboard(request):
    require(request.user, "view_company")
    tasks = scope_queryset(request.user, "view_tasks", Task.objects.all())
    since = timezone.now() - timedelta(days=30)
    recent = tasks.filter(updated_at__gte=since)
    done, failed = recent.filter(status=Task.COMPLETED).count(), recent.filter(status=Task.FAILED).count()
    usage = UsageRecord.objects.filter(created_at__gte=since)
    company_budgets = Budget.objects.filter(scope=Budget.COMPANY, is_active=True)
    ctx = {
        "employee_states": _employee_state_counts(request.user),
        "employee_total": scope_queryset(request.user, "view_employees", EmployeeProfile.objects.all(), project_field=None).count(),
        "task_counts": dict(tasks.values_list("status").annotate(n=Count("id"))),
        "completion_rate": round(100 * done / (done + failed)) if done + failed else None,
        "done_30d": done, "failed_30d": failed,
        "approvals": ApprovalRequest.objects.filter(status=ApprovalRequest.PENDING).count() if can(request.user, "manage_tasks") else None,
        "under_review": tasks.filter(status=Task.UNDER_REVIEW).select_related("assigned_employee")[:8],
        "blockers": tasks.filter(status__in=[Task.BLOCKED, Task.WAITING, Task.FAILED, Task.PAUSED]).select_related("assigned_employee")[:8],
        "running": tasks.filter(status=Task.RUNNING).select_related("assigned_employee")[:8],
        "usage_calls": usage.count(), "usage_failures": usage.filter(success=False).count(),
        "usage_fallbacks": usage.filter(was_fallback=True, success=True).count(),
        "usage_cost": usage.aggregate(c=Sum("estimated_cost_usd"))["c"] or Decimal("0"),
        "providers": ModelProvider.objects.all() if can(request.user, "view_models") else [],
        "budgets": [(b, budget.spent(b)) for b in company_budgets],
        "incidents": Incident.objects.filter(is_resolved=False)[:5] if request.user.is_founder or request.user.is_ceo else [],
        "recent_events": AuditEvent.objects.select_related()[:10] if can(request.user, "view_audit") else [],
        "workers_alive": _workers_alive(),
    }
    return render(request, "dashboard.html", ctx)


# ------------------------------------------------------------------ departments

def department_list(request):
    require(request.user, "view_department")
    depts = scope_queryset(request.user, "view_department", Department.objects.all(), dept_field="pk", project_field=None)
    depts = depts.annotate(n_emp=Count("employees", distinct=True),
                           n_open=Count("tasks", filter=~Q(tasks__status__in=Task.TERMINAL), distinct=True))
    form = None
    if can(request.user, "manage_departments"):
        form = DepartmentForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            d = form.save()
            audit.record("department.created", target=d, request=request, details={"name": d.name})
            messages.success(request, f"Department {d.name} created.")
            return redirect("department_detail", d.pk)
    return render(request, "departments/list.html", {"departments": depts, "form": form})


def department_detail(request, pk):
    dept = get_object_or_404(Department, pk=pk)
    require(request.user, "view_department", dept)
    form = DepartmentForm(request.POST or None, instance=dept) if can(request.user, "manage_departments") else None
    if request.method == "POST":
        require(request.user, "manage_departments")
        action = request.POST.get("action")
        if action in ("pause", "resume"):
            dept.is_paused = action == "pause"
            dept.save(update_fields=["is_paused"])
            audit.record(f"department.{action}d", target=dept, request=request)
            messages.success(request, f"Department {action}d.")
            return redirect("department_detail", pk)
        if form.is_valid():
            form.save()
            audit.record("department.updated", target=dept, request=request, details={"fields": list(form.changed_data)})
            messages.success(request, "Department updated.")
            return redirect("department_detail", pk)
    tasks = scope_queryset(request.user, "view_tasks", dept.tasks.all()).select_related("assigned_employee")
    return render(request, "departments/detail.html", {
        "dept": dept, "form": form, "employees": dept.employees.all(),
        "tasks": tasks.exclude(status__in=Task.TERMINAL)[:50],
        "stats": dict(tasks.values_list("status").annotate(n=Count("id"))),
        "tool_grants": ToolPermission.objects.filter(scope="department", department=dept).select_related("tool"),
    })


# ------------------------------------------------------------------ employees

def employee_list(request):
    require(request.user, "view_employees")
    qs = scope_queryset(request.user, "view_employees", EmployeeProfile.objects.select_related("department"), project_field=None)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(job_title__icontains=q) | Q(employee_id__icontains=q))
    if request.GET.get("department"):
        qs = qs.filter(department_id=request.GET["department"])
    from django.db.models import Prefetch

    from apps.office.state import OPEN_STATUSES, employee_visual_state

    qs = qs.prefetch_related(
        "model_policies__model",
        Prefetch("tasks", queryset=Task.objects.filter(status__in=OPEN_STATUSES).order_by("priority", "-updated_at"), to_attr="open_tasks"))
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    rows = []
    for e in page:
        state, task = employee_visual_state(e, e.open_tasks)
        rows.append((e, state, task))
    return render(request, "employees/list.html", {"page": page, "rows": rows, "q": q, "departments": Department.objects.all()})


def employee_create(request):
    require(request.user, "manage_employees")
    initial = {}
    if request.GET.get("template"):
        tpl = RoleTemplate.objects.filter(pk=request.GET["template"]).first()
        if tpl:
            initial = {"role_template": tpl, "job_title": tpl.job_title}
    form = EmployeeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        emp = form.save()
        audit.record("employee.created", target=emp, request=request, details={"employee_id": emp.employee_id, "title": emp.job_title})
        messages.success(request, f"{emp.name} ({emp.employee_id}) created. Configure model priority and tools next.")
        return redirect("employee_detail", emp.pk)
    return render(request, "employees/form.html", {"form": form, "templates": RoleTemplate.objects.all()})


def employee_detail(request, pk):
    emp = get_object_or_404(EmployeeProfile.objects.select_related("department"), pk=pk)
    require(request.user, "view_employees", emp)
    manage = can(request.user, "manage_employees")
    form = EmployeeForm(request.POST or None, instance=emp) if manage else None
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action in ("pause", "resume"):
            require(request.user, "control_employees", emp)
            emp.status = EmployeeProfile.PAUSED if action == "pause" else EmployeeProfile.ACTIVE
            emp.save(update_fields=["status"])
            audit.record(f"employee.{action}d", target=emp, request=request)
            messages.success(request, f"{emp.name} {action}d.")
            return redirect("employee_detail", pk)
        require(request.user, "manage_employees")
        if action == "suspend":
            emp.status = EmployeeProfile.SUSPENDED
            emp.save(update_fields=["status"])
            audit.record("employee.suspended", target=emp, request=request)
            return redirect("employee_detail", pk)
        if action == "add_model":
            model = get_object_or_404(AIModel, pk=request.POST.get("model"))
            prio = (emp.model_policies.order_by("-priority").values_list("priority", flat=True).first() or 0) + 1
            try:
                EmployeeModelPolicy.objects.create(employee=emp, model=model, priority=prio, allow_paid=request.POST.get("allow_paid") == "on")
                audit.record("employee.model_policy_added", target=emp, request=request, details={"model": str(model), "priority": prio})
            except IntegrityError:
                messages.error(request, "That model is already in this employee's priority list.")
            return redirect("employee_detail", pk)
        if action in ("model_up", "model_down", "model_remove"):
            _edit_model_policy(emp, int(request.POST["policy"]), action)
            audit.record("employee.model_policy_changed", target=emp, request=request, details={"action": action})
            return redirect("employee_detail", pk)
        if action in ("grant_tool", "deny_tool"):
            require(request.user, "manage_tools")
            tool = get_object_or_404(ToolDefinition, pk=request.POST.get("tool"))
            ToolPermission.objects.filter(scope="employee", employee=emp, tool=tool).delete()
            ToolPermission.objects.create(tool=tool, scope="employee", employee=emp, allow=action == "grant_tool", granted_by=request.user)
            audit.record(f"tool.{'granted' if action == 'grant_tool' else 'denied'}", target=emp, request=request, details={"tool": tool.slug})
            return redirect("employee_detail", pk)
        if action == "revoke_tool":
            require(request.user, "manage_tools")
            ToolPermission.objects.filter(pk=request.POST.get("grant"), scope="employee", employee=emp).delete()
            audit.record("tool.revoked", target=emp, request=request, details={"grant": request.POST.get("grant")})
            return redirect("employee_detail", pk)
        if form is not None and form.is_valid():
            form.save()
            audit.record("employee.updated", target=emp, request=request, details={"fields": list(form.changed_data)})
            messages.success(request, "Employee updated.")
            return redirect("employee_detail", pk)
    tasks = scope_queryset(request.user, "view_tasks", emp.tasks.all())
    usage = emp.usage.all()
    finished = tasks.filter(status__in=[Task.COMPLETED, Task.FAILED, Task.REVISION])
    reviewed = tasks.filter(reviews__isnull=False).distinct()
    metrics = {
        "completed": tasks.filter(status=Task.COMPLETED).count(),
        "failed": tasks.filter(status=Task.FAILED).count(),
        "accepted": reviewed.filter(reviews__outcome="accept").count(),
        "rejected": reviewed.filter(reviews__outcome="reject").count(),
        "calls": usage.count(), "cost": usage.aggregate(c=Sum("estimated_cost_usd"))["c"] or Decimal("0"),
        "finished": finished.count(),
    }
    return render(request, "employees/detail.html", {
        "emp": emp, "form": form, "manage": manage,
        "policies": emp.model_policies.select_related("model__provider"),
        "available_models": AIModel.objects.filter(is_enabled=True).select_related("provider"),
        "tool_grants": emp.tool_permissions.select_related("tool"),
        "tools": ToolDefinition.objects.filter(has_handler=True),
        "tasks": tasks.order_by("-updated_at")[:25], "metrics": metrics,
    })


def _edit_model_policy(emp, policy_id, action):
    policies = list(emp.model_policies.order_by("priority"))
    idx = next((i for i, p in enumerate(policies) if p.pk == policy_id), None)
    if idx is None:
        return
    if action == "model_remove":
        policies.pop(idx).delete()
    elif action == "model_up" and idx > 0:
        policies[idx - 1], policies[idx] = policies[idx], policies[idx - 1]
    elif action == "model_down" and idx < len(policies) - 1:
        policies[idx + 1], policies[idx] = policies[idx], policies[idx + 1]
    with transaction.atomic():  # two-phase renumber to respect the unique (employee, priority) constraint
        for i, p in enumerate(policies):
            EmployeeModelPolicy.objects.filter(pk=p.pk).update(priority=1000 + i)
        for i, p in enumerate(policies):
            EmployeeModelPolicy.objects.filter(pk=p.pk).update(priority=i + 1)


def role_templates(request):
    require(request.user, "view_employees")
    form = RoleTemplateForm(request.POST or None) if can(request.user, "manage_employees") else None
    if request.method == "POST":
        require(request.user, "manage_employees")
        if form.is_valid():
            t = form.save()
            audit.record("role_template.created", target=t, request=request)
            messages.success(request, "Role template created.")
            return redirect("role_templates")
    return render(request, "employees/templates.html", {"templates": RoleTemplate.objects.annotate(n=Count("employees")), "form": form})


# ------------------------------------------------------------------ settings, users, budgets

def company_settings(request):
    require(request.user, "manage_settings")
    company = CompanySettings.get()
    form = CompanySettingsForm(request.POST or None, instance=company)
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action in ("stop", "unstop"):
            require(request.user, "emergency_stop")
            company.emergency_stop = action == "stop"
            company.emergency_stop_reason = request.POST.get("reason", "")[:500] if action == "stop" else ""
            company.save(update_fields=["emergency_stop", "emergency_stop_reason"])
            audit.record("company.emergency_stop" if action == "stop" else "company.emergency_stop_lifted", request=request,
                         details={"reason": company.emergency_stop_reason})
            if action == "stop":
                Incident.objects.create(title="Emergency stop activated", severity="critical", category="operations",
                                        description=company.emergency_stop_reason)
                from .notify import notify_leadership

                notify_leadership("security", "Emergency stop activated", company.emergency_stop_reason, "/settings/")
            messages.warning(request, "Emergency stop " + ("ACTIVATED" if action == "stop" else "lifted"))
            return redirect("company_settings")
        if action in ("enable_paid", "disable_paid"):
            company.paid_models_enabled = action == "enable_paid"
            company.save(update_fields=["paid_models_enabled"])
            audit.record("company.paid_models", request=request, details={"enabled": company.paid_models_enabled})
            messages.success(request, "Paid model usage " + ("enabled" if company.paid_models_enabled else "disabled"))
            return redirect("company_settings")
        if form.is_valid():
            old_ceo = (company.ceo_user_id, company.ceo_employee_id)
            form.save()
            audit.record("company.settings_updated", request=request, details={"fields": list(form.changed_data)})
            if old_ceo != (company.ceo_user_id, company.ceo_employee_id):
                audit.record("company.ceo_appointed", request=request, details={"user": company.ceo_user_id, "employee": company.ceo_employee_id})
            messages.success(request, "Settings saved.")
            return redirect("company_settings")
    return render(request, "settings/company.html", {"form": form, "company": company})


def users_view(request):
    require(request.user, "manage_permissions")
    uform = UserCreateForm(request.POST if request.POST.get("action") == "create_user" else None)
    pform = PermissionAssignmentForm(request.POST if request.POST.get("action") == "grant" else None)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create_user" and uform.is_valid():
            u = uform.save()
            audit.record("account.created", target=u, request=request, details={"type": u.account_type})
            messages.success(request, f"Account {u.username} created.")
            return redirect("users")
        if action == "grant" and pform.is_valid():
            try:
                pa = pform.save(commit=False)
                pa.granted_by = request.user
                pa.save()
                audit.record("permission.granted", target=pa.user, request=request,
                             details={"permission": pa.permission, "department": pa.department_id, "project": pa.project_id})
                messages.success(request, "Permission granted.")
            except IntegrityError:
                messages.error(request, "That permission is already granted.")
            return redirect("users")
        if action == "revoke":
            pa = get_object_or_404(PermissionAssignment, pk=request.POST.get("assignment"))
            audit.record("permission.revoked", target=pa.user, request=request, details={"permission": pa.permission})
            pa.delete()
            return redirect("users")
        if action in ("deactivate", "activate"):
            u = get_object_or_404(User, pk=request.POST.get("user"))
            if u.is_founder or u == request.user:
                messages.error(request, "The Founder account cannot be deactivated here.")
            else:
                u.is_active = action == "activate"
                u.save(update_fields=["is_active"])
                audit.record(f"account.{action}d", target=u, request=request)
            return redirect("users")
    return render(request, "settings/users.html", {
        "users": User.objects.order_by("account_type", "username"), "uform": uform, "pform": pform,
        "assignments": PermissionAssignment.objects.select_related("user", "department", "project"),
    })


def budgets_view(request):
    require(request.user, "view_reports")
    form = BudgetForm(request.POST or None) if can(request.user, "manage_budgets") else None
    if request.method == "POST":
        require(request.user, "manage_budgets")
        if request.POST.get("action") == "deactivate":
            b = get_object_or_404(Budget, pk=request.POST.get("budget"))
            b.is_active = False
            b.save(update_fields=["is_active"])
            audit.record("budget.deactivated", target=b, request=request)
            return redirect("budgets")
        if form.is_valid():
            b = form.save()
            audit.record("budget.created", target=b, request=request, details={"scope": b.scope, "limit": str(b.limit_usd)})
            messages.success(request, "Budget created.")
            return redirect("budgets")
    rows = [(b, budget.spent(b)) for b in Budget.objects.filter(is_active=True).select_related("department", "employee", "project", "provider")]
    return render(request, "settings/budgets.html", {"rows": rows, "form": form, "company": CompanySettings.get()})


# ------------------------------------------------------------------ audit & notifications

def audit_log(request):
    require(request.user, "view_audit")
    qs = AuditEvent.objects.all()
    for field in ("action", "target_type"):
        if request.GET.get(field):
            qs = qs.filter(**{f"{field}__icontains": request.GET[field]})
    if request.GET.get("actor"):
        qs = qs.filter(actor_label__icontains=request.GET["actor"])
    verified = None
    if request.GET.get("verify") == "1":
        verified = audit.verify_chain()
    return render(request, "audit/list.html", {"page": Paginator(qs, 50).get_page(request.GET.get("page")), "verified": verified})


def notification_preferences(request):
    from .models import NotificationPreference as NP

    company = CompanySettings.get()
    current = {p.kind: p for p in request.user.notification_preferences.all()}
    if request.method == "POST":
        for kind, _ in NP.KINDS:
            in_app = kind in NP.MANDATORY or request.POST.get(f"in_app_{kind}") == "on"
            email = request.POST.get(f"email_{kind}") == "on"
            NP.objects.update_or_create(user=request.user, kind=kind, defaults={"in_app": in_app, "email": email})
        audit.record("notifications.preferences_updated", request=request)
        messages.success(request, "Notification preferences saved.")
        return redirect("notification_preferences")
    rows = [(kind, label, kind in NP.MANDATORY, current[kind].in_app if kind in current else True,
             current[kind].email if kind in current else False) for kind, label in NP.KINDS]
    return render(request, "notification_prefs.html", {"rows": rows, "email_enabled": company.email_notifications_enabled,
                                                       "has_email": bool(request.user.email)})


def notifications(request):
    if request.method == "POST":
        request.user.notifications.filter(is_read=False).update(is_read=True)
        return redirect("notifications")
    return render(request, "notifications.html", {"page": Paginator(request.user.notifications.all(), 30).get_page(request.GET.get("page"))})
