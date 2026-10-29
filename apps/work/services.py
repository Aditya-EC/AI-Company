"""Task lifecycle service. All status changes go through `transition`, which validates the
state machine, uses a conditional UPDATE (race-safe), records history and audit events."""
from django.db import transaction
from django.utils import timezone

from apps.core import audit
from apps.core.notify import notify_leadership, notify_user

from .events import publish_task_event
from .models import ApprovalRequest, Review, Task, TaskStatusChange

T = Task
ALLOWED = {
    T.DRAFT: {T.PROPOSED, T.QUEUED, T.BLOCKED, T.AWAITING_APPROVAL, T.CANCELLED},
    T.PROPOSED: {T.APPROVED, T.CANCELLED},
    T.APPROVED: {T.QUEUED, T.BLOCKED, T.AWAITING_APPROVAL, T.CANCELLED},
    T.AWAITING_APPROVAL: {T.QUEUED, T.BLOCKED, T.CANCELLED},
    T.BLOCKED: {T.QUEUED, T.PAUSED, T.CANCELLED},
    T.QUEUED: {T.ASSIGNED, T.RUNNING, T.BLOCKED, T.PAUSED, T.WAITING, T.CANCELLED},
    T.ASSIGNED: {T.RUNNING, T.QUEUED, T.PAUSED, T.CANCELLED},
    T.RUNNING: {T.UNDER_REVIEW, T.COMPLETED, T.FAILED, T.QUEUED, T.WAITING, T.PAUSED, T.BLOCKED, T.CANCELLED},
    T.WAITING: {T.QUEUED, T.CANCELLED},
    T.UNDER_REVIEW: {T.COMPLETED, T.REVISION, T.CANCELLED},
    T.REVISION: {T.COMPLETED, T.QUEUED, T.CANCELLED},
    T.PAUSED: {T.QUEUED, T.CANCELLED},
    T.FAILED: {T.QUEUED, T.CANCELLED},
    T.COMPLETED: set(),
    T.CANCELLED: set(),
}


class InvalidTransition(Exception):
    pass


def _label(user=None, employee=None):
    if employee is not None:
        return f"agent:{employee.employee_id}"
    if user is not None:
        return f"user:{user.username}"
    return "engine"


def transition(task, to_status, *, user=None, employee=None, reason="", extra=None):
    """Move task to `to_status` if allowed and if nobody changed it concurrently."""
    from_status = task.status
    if to_status not in ALLOWED.get(from_status, set()):
        raise InvalidTransition(f"{task.code}: {from_status} -> {to_status} is not allowed")
    now = timezone.now()
    fields = {"status": to_status, "status_reason": reason[:500], "updated_at": now}
    if to_status == T.QUEUED:
        fields["queued_at"] = now
    if to_status in T.TERMINAL or to_status == T.UNDER_REVIEW:
        fields.update(lease_owner="", lease_expires_at=None)
    if to_status in T.TERMINAL:
        fields["completed_at"] = now
    if to_status in (T.PAUSED, T.QUEUED, T.WAITING, T.BLOCKED):
        fields.update(lease_owner="", lease_expires_at=None)
    fields.update(extra or {})
    with transaction.atomic():
        updated = Task.objects.filter(pk=task.pk, status=from_status).update(**fields)
        if not updated:
            raise InvalidTransition(f"{task.code} changed concurrently (expected {from_status})")
        TaskStatusChange.objects.create(task=task, from_status=from_status, to_status=to_status,
                                        actor_label=_label(user, employee), reason=reason[:500])
        audit.record("task.status", user=user, employee=employee, target=task,
                     details={"from": from_status, "to": to_status, "reason": reason[:300]})
    for k, v in fields.items():
        setattr(task, k, v)
    transaction.on_commit(lambda: publish_task_event(task))
    return task


def unmet_dependencies(task):
    return [d.depends_on for d in task.dependencies.select_related("depends_on") if d.depends_on.status != T.COMPLETED]


def dependency_creates_cycle(task, depends_on):
    """True if task is reachable from depends_on's own dependencies (would form a cycle)."""
    seen, stack = set(), [depends_on.pk]
    while stack:
        cur = stack.pop()
        if cur == task.pk:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(Task.objects.get(pk=cur).dependencies.values_list("depends_on_id", flat=True))
    return False


def queue_task(task, *, user=None, reason="queued"):
    """Queue a task for execution, respecting dependencies and approval requirements."""
    if task.requires_approval and not task.approvals.filter(kind="task_execution", status=ApprovalRequest.APPROVED).exists():
        if task.status != T.AWAITING_APPROVAL:
            transition(task, T.AWAITING_APPROVAL, user=user, reason="execution requires approval")
            if not task.approvals.filter(kind="task_execution", status=ApprovalRequest.PENDING).exists():
                ApprovalRequest.objects.create(kind="task_execution", title=f"Approve execution of {task.code}: {task.title}",
                                               description=task.description[:2000], task=task, project=task.project,
                                               requested_by_label=_label(user))
                notify_leadership("approval", f"Approval requested: {task.code}", task.title, f"/approvals/")
        return task
    blockers = unmet_dependencies(task)
    if blockers:
        if task.status != T.BLOCKED:
            transition(task, T.BLOCKED, user=user, reason="waiting on " + ", ".join(b.code for b in blockers))
        return task
    return transition(task, T.QUEUED, user=user, reason=reason)


def cancel_task(task, *, user, reason="cancelled by user"):
    if task.status == T.RUNNING:
        # Cooperative cancellation: the engine stops at the next safe checkpoint.
        Task.objects.filter(pk=task.pk).update(cancel_requested=True)
        audit.record("task.cancel_requested", user=user, target=task)
        task.cancel_requested = True
        return task
    return transition(task, T.CANCELLED, user=user, reason=reason)


def pause_task(task, *, user, reason="paused by user"):
    if task.status == T.RUNNING:
        Task.objects.filter(pk=task.pk).update(pause_requested=True)
        audit.record("task.pause_requested", user=user, target=task)
        task.pause_requested = True
        return task
    return transition(task, T.PAUSED, user=user, reason=reason)


def resume_task(task, *, user):
    Task.objects.filter(pk=task.pk).update(pause_requested=False, cancel_requested=False)
    task.pause_requested = task.cancel_requested = False
    return queue_task(task, user=user, reason="resumed")


def retry_task(task, *, user):
    if task.status not in (T.FAILED, T.WAITING, T.PAUSED):
        raise InvalidTransition("Only failed, paused or waiting tasks can be retried")
    return queue_task(task, user=user, reason="manual retry")


def change_priority(task, priority, *, user, reason=""):
    old = task.priority
    Task.objects.filter(pk=task.pk).update(priority=priority)
    task.priority = priority
    audit.record("task.priority", user=user, target=task, details={"from": old, "to": priority, "reason": reason})
    publish_task_event(task)
    return task


def reassign(task, employee, *, user):
    if task.status == T.RUNNING:
        raise InvalidTransition("Cannot reassign a running task; pause it first")
    old = task.assigned_employee_id
    Task.objects.filter(pk=task.pk).update(assigned_employee=employee)
    task.assigned_employee = employee
    audit.record("task.reassigned", user=user, target=task, details={"from": old, "to": employee.pk if employee else None})
    publish_task_event(task)
    return task


def review_task(task, *, user, outcome, comments=""):
    """Accept or reject a deliverable. Rejection creates a specific revision task."""
    if task.status != T.UNDER_REVIEW:
        raise InvalidTransition("Task is not under review")
    if task.assigned_employee and task.assigned_employee.user_account_id == user.pk:
        raise InvalidTransition("The author cannot review their own work")
    with transaction.atomic():
        review = Review.objects.create(task=task, reviewer=user, outcome=outcome, comments=comments)
        audit.record("task.reviewed", user=user, target=task, details={"outcome": outcome})
        if outcome == Review.ACCEPT:
            transition(task, T.COMPLETED, user=user, reason="accepted in review")
            if task.parent_task_id and task.parent_task.status == T.REVISION:
                transition(task.parent_task, T.COMPLETED, user=user, reason=f"superseded by accepted revision {task.code}")
            return review, None
        transition(task, T.REVISION, user=user, reason="rejected in review")
        revision = Task.objects.create(
            title=f"Revision: {task.title}"[:200],
            description=f"{task.description}\n\nREVIEW FEEDBACK (must be addressed):\n{comments}\n\nPREVIOUS OUTPUT:\n{task.output[:4000]}",
            acceptance_criteria=task.acceptance_criteria, project=task.project, department=task.department,
            assigned_employee=task.assigned_employee, parent_task=task, created_by=user, priority=task.priority,
            requires_review=True, reviewer=task.reviewer, is_confidential=task.is_confidential,
            required_capabilities=task.required_capabilities, max_retries=task.max_retries, max_steps=task.max_steps,
            max_runtime_seconds=task.max_runtime_seconds, max_cost_usd=task.max_cost_usd,
        )
        audit.record("task.revision_created", user=user, target=revision, details={"parent": task.pk})
    queue_task(revision, user=user, reason="revision after rejected review")
    notify_user(task.created_by, "review", f"{task.code} rejected; revision {revision.code} queued", comments[:500])
    return review, revision


def decide_approval(approval, *, user, approve, note=""):
    from apps.core.permissions import can

    if approval.status != ApprovalRequest.PENDING:
        raise InvalidTransition("Approval already decided")
    if approval.required_authority == "founder" and not user.is_founder:
        raise InvalidTransition("Only the Founder can decide this approval")
    if not can(user, "manage_tasks", approval.task):
        raise InvalidTransition("Not authorized to decide approvals")
    approval.status = ApprovalRequest.APPROVED if approve else ApprovalRequest.REJECTED
    approval.decided_by, approval.decided_at, approval.decision_note = user, timezone.now(), note
    approval.save()
    audit.record("approval.decided", user=user, target=approval, details={"approved": approve, "kind": approval.kind})
    if approval.task_id and approval.kind == "task_execution":
        task = approval.task
        if approve:
            queue_task(task, user=user, reason="approved")
        else:
            transition(task, T.CANCELLED, user=user, reason=f"approval rejected: {note}"[:500])
    return approval
