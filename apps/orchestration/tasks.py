"""Celery entry points. The engine logic lives in engine.py; Celery is only a transport."""
from celery import shared_task
from django.db import close_old_connections

from . import engine


@shared_task(name="apps.orchestration.tasks.engine_tick")
def engine_tick():
    """Periodic: recover, unblock and dispatch queued tasks to workers (claim happens in the worker)."""
    worker = f"celery-beat:{engine.socket.gethostname()}"
    engine.heartbeat(worker, kind="celery")
    engine.recover_interrupted()
    engine.unblock_ready_tasks()
    engine.warn_deadlines()
    from apps.knowledge.meetings import recover_meetings
    from apps.knowledge.models import Meeting

    recover_meetings()
    for tid in engine.next_queued_ids(engine._capacity_available()):
        execute_queued_task.delay(tid)
    for mid in Meeting.objects.filter(status=Meeting.QUEUED).values_list("id", flat=True)[:2]:
        run_queued_meeting.delay(mid)
    return worker


@shared_task(name="apps.orchestration.tasks.run_queued_meeting", acks_late=True)
def run_queued_meeting(meeting_id):
    from apps.knowledge.meetings import claim_meeting

    close_old_connections()
    worker = engine.new_worker_id()
    meeting = claim_meeting(meeting_id, worker)  # conditional claim prevents duplicate runs
    if meeting:
        engine.run_meeting(meeting, worker)
    return meeting_id


@shared_task(name="apps.orchestration.tasks.execute_queued_task", acks_late=True)
def execute_queued_task(task_id):
    close_old_connections()
    worker = engine.new_worker_id()
    task = engine.claim_task(task_id, worker)  # conditional claim prevents duplicate execution
    if task:
        engine.execute_task(task.pk, worker)
    return task_id
