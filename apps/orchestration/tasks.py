"""Celery entry points. The engine logic lives in engine.py; Celery is only a transport."""
from celery import shared_task
from django.db import close_old_connections

from . import engine


@shared_task(name="apps.orchestration.tasks.engine_tick")
def engine_tick():
    """Periodic: recover, unblock and dispatch queued tasks to workers (claim happens in the worker)."""
    worker = engine.new_worker_id()
    engine.recover_interrupted()
    engine.unblock_ready_tasks()
    for tid in engine.next_queued_ids(engine._capacity_available()):
        execute_queued_task.delay(tid)
    return worker


@shared_task(name="apps.orchestration.tasks.execute_queued_task", acks_late=True)
def execute_queued_task(task_id):
    close_old_connections()
    worker = engine.new_worker_id()
    task = engine.claim_task(task_id, worker)  # conditional claim prevents duplicate execution
    if task:
        engine.execute_task(task.pk, worker)
    return task_id
