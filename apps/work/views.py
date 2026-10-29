from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.forms import TaskForm
from apps.core.models import CompanySettings, EmployeeProfile
from apps.core.permissions import can, require, scope_queryset

from . import services
from .models import ApprovalRequest, Priority, Review, Task, TaskDependency

BOARD_COLUMNS = [
    ("Backlog", [Task.DRAFT, Task.PROPOSED, Task.APPROVED, Task.AWAITING_APPROVAL]),
    ("Queued", [Task.QUEUED, Task.ASSIGNED, Task.BLOCKED]),
    ("Running", [Task.RUNNING, Task.WAITING, Task.PAUSED]),
    ("Review", [Task.UNDER_REVIEW, Task.REVISION]),
    ("Done", [Task.COMPLETED, Task.FAILED, Task.CANCELLED]),
]


def task_board(request):
    require(request.user, "view_tasks")
    qs = scope_queryset(request.user, "view_tasks", Task.objects.select_related("assigned_employee", "department", "project"))
    if request.GET.get("department"):
        qs = qs.filter(department_id=request.GET["department"])
    if request.GET.get("q"):
        qs = qs.filter(title__icontains=request.GET["q"])
    columns = [(name, [t for t in qs.filter(status__in=sts).order_by("priority", "-updated_at")[:60]]) for name, sts in BOARD_COLUMNS]
    return render(request, "tasks/board.html", {"columns": columns, "can_create": can(request.user, "manage_tasks")})


def task_create(request):
    require(request.user, "manage_tasks")
    company = CompanySettings.get()
    form = TaskForm(request.POST or None, user=request.user, initial={
        "max_steps": company.default_max_steps, "max_retries": company.default_max_retries,
        "max_runtime_seconds": company.default_max_runtime_seconds, "priority": Priority.NORMAL})
    if request.method == "POST" and form.is_valid():
        require(request.user, "manage_tasks", form.cleaned_data["department"])
        with transaction.atomic():
            task = form.save(commit=False)
            task.created_by = request.user
            if task.assigned_employee and task.assigned_employee.role_template:
                task.required_capabilities = list(task.assigned_employee.role_template.required_capabilities)
            task.save()
            for dep in form.cleaned_data["depends_on"]:
                TaskDependency.objects.create(task=task, depends_on=dep)
            audit.record("task.created", target=task, request=request, details={"title": task.title})
        if request.POST.get("submit") == "queue":
            services.queue_task(task, user=request.user)
            messages.success(request, f"{task.code} created and submitted to the engine ({task.get_status_display()}).")
        else:
            messages.success(request, f"{task.code} saved as draft.")
        return redirect("task_detail", task.pk)
    return render(request, "tasks/form.html", {"form": form})


def task_detail(request, pk):
    task = get_object_or_404(Task.objects.select_related("assigned_employee", "department", "project", "reviewer", "created_by"), pk=pk)
    require(request.user, "view_tasks", task)
    executions = task.executions.select_related("model_used").prefetch_related("tool_calls")
    from apps.core.models import AuditEvent

    return render(request, "tasks/detail.html", {
        "task": task, "executions": executions, "history": task.status_history.all(),
        "artifacts": task.artifacts.all(), "reviews": task.reviews.select_related("reviewer"),
        "usage": task.usage.select_related("model", "provider"), "dependencies": task.dependencies.select_related("depends_on"),
        "approvals": task.approvals.all(), "revisions": task.revisions.all(),
        "audit_events": AuditEvent.objects.filter(target_type="work.task", target_id=str(task.pk))[:30] if can(request.user, "view_audit") else None,
        "can_manage": can(request.user, "manage_tasks", task), "can_review": can(request.user, "review_tasks", task),
        "priorities": Priority.choices,
        "employees": EmployeeProfile.objects.filter(department=task.department, status="active"),
    })


@require_POST
def task_action(request, pk):
    task = get_object_or_404(Task, pk=pk)
    action = request.POST.get("action")
    perm = "review_tasks" if action == "review" else "manage_tasks"
    require(request.user, perm, task)
    try:
        if action == "queue":
            services.queue_task(task, user=request.user)
        elif action == "cancel":
            services.cancel_task(task, user=request.user, reason=request.POST.get("reason") or "cancelled by user")
        elif action == "pause":
            services.pause_task(task, user=request.user)
        elif action == "resume":
            services.resume_task(task, user=request.user)
        elif action == "retry":
            services.retry_task(task, user=request.user)
        elif action == "priority":
            services.change_priority(task, int(request.POST["priority"]), user=request.user, reason=request.POST.get("reason", ""))
        elif action == "reassign":
            emp = get_object_or_404(EmployeeProfile, pk=request.POST.get("employee"), department=task.department)
            services.reassign(task, emp, user=request.user)
        elif action == "review":
            outcome = request.POST.get("outcome")
            if outcome not in (Review.ACCEPT, Review.REJECT):
                raise services.InvalidTransition("Choose accept or reject")
            if outcome == Review.REJECT and not request.POST.get("comments", "").strip():
                raise services.InvalidTransition("Rejections need specific feedback for the revision task")
            _, revision = services.review_task(task, user=request.user, outcome=outcome, comments=request.POST.get("comments", ""))
            if revision:
                messages.info(request, f"Revision task {revision.code} created.")
        else:
            raise services.InvalidTransition("Unknown action")
        task.refresh_from_db()
        messages.success(request, f"{task.code}: {task.get_status_display()}" + (" (stop requested at next checkpoint)" if task.cancel_requested or task.pause_requested else ""))
    except services.InvalidTransition as exc:
        messages.error(request, str(exc))
    return redirect("task_detail", pk)


def approvals(request):
    if not can(request.user, "manage_tasks"):
        raise PermissionDenied
    if request.method == "POST":
        ap = get_object_or_404(ApprovalRequest, pk=request.POST.get("approval"))
        try:
            services.decide_approval(ap, user=request.user, approve=request.POST.get("decision") == "approve", note=request.POST.get("note", ""))
            messages.success(request, f"Approval {ap.get_status_display().lower()}.")
        except services.InvalidTransition as exc:
            messages.error(request, str(exc))
        return redirect("approvals")
    return render(request, "approvals/list.html", {
        "pending": ApprovalRequest.objects.filter(status=ApprovalRequest.PENDING).select_related("task"),
        "decided": ApprovalRequest.objects.exclude(status=ApprovalRequest.PENDING).select_related("task", "decided_by")[:30],
    })
