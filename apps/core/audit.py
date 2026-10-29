"""Append-only, hash-chained audit logging."""
import logging

from django.db import IntegrityError, transaction

from .models import AuditEvent
from .redaction import redact

log = logging.getLogger("vcoos.audit")
GENESIS = "0" * 64


def _actor_label(user=None, employee=None):
    if employee is not None:
        return f"agent:{employee.employee_id}:{employee.name}"
    if user is not None and getattr(user, "is_authenticated", False):
        return f"user:{user.pk}:{user.username}"
    return "system"


def record(action, *, user=None, employee=None, target=None, details=None, request=None):
    """Append an audit event. Retries if a concurrent writer extended the chain first."""
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    ip = None
    if request is not None:
        ip = request.META.get("REMOTE_ADDR")
        user = user or (request.user if request.user.is_authenticated else None)
    target_type = target_id = ""
    if target is not None:
        target_type = target._meta.label_lower
        target_id = str(target.pk)
    for _ in range(5):
        try:
            with transaction.atomic():
                last = AuditEvent.objects.select_for_update().order_by("-id").first()
                ev = AuditEvent(
                    actor_user=user,
                    actor_employee=employee,
                    actor_label=_actor_label(user, employee),
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    details=redact(details or {}),
                    ip_address=ip,
                    prev_hash=last.hash if last else GENESIS,
                )
                ev.hash = ev.compute_hash()
                ev.save()
                return ev
        except IntegrityError:
            continue
    raise RuntimeError("Could not append audit event after retries")


def verify_chain():
    """Return (ok, first_bad_event_id). Detects edits, deletions and re-ordering."""
    prev = GENESIS
    for ev in AuditEvent.objects.order_by("id").iterator():
        if ev.prev_hash != prev or ev.compute_hash() != ev.hash:
            return False, ev.id
        prev = ev.hash
    return True, None
