"""Publish real backend state changes to WebSocket subscribers (best effort; never blocks work)."""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

log = logging.getLogger("vcoos.events")
COMPANY_GROUP = "company-activity"


def publish(event_type, payload):
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(COMPANY_GROUP, {"type": "activity.event", "event": event_type, "payload": payload})
    except Exception as exc:  # channel layer down must not break task processing
        log.warning("event publish failed: %s", exc)


def publish_task_event(task):
    publish("task", {
        "id": task.pk,
        "code": task.code,
        "title": task.title,
        "status": task.status,
        "department_id": task.department_id,
        "employee_id": task.assigned_employee_id,
        "progress_note": task.progress_note,
    })
