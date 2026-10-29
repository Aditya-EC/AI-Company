"""Concurrency/load: several engine workers on separate threads and DB connections share one queue.

Checks: no task executes twice, every task reaches its resting state, per-employee concurrency is
respected, and the audit hash chain stays valid under concurrent appends. Most meaningful on
PostgreSQL (row locks); also runs on SQLite (serialized writes).
"""
import json
import threading
import time

import pytest
from django.db import close_old_connections, connection

from apps.core.audit import verify_chain
from apps.core.models import CompanySettings, Department, EmployeeProfile
from apps.gateway.models import EmployeeModelPolicy
from apps.orchestration import engine
from apps.work import services
from apps.work.models import Task, TaskExecution, TaskStatusChange

from .conftest import make_model, make_provider

N_EMPLOYEES, N_TASKS, N_WORKERS = 6, 60, 8


@pytest.mark.django_db(transaction=True)
def test_concurrent_workers_no_duplicates_and_consistent_audit(llm, settings):
    settings.VCOOS_TASK_LEASE_SECONDS = 300
    lock = threading.Lock()
    running = {}
    peak = {}

    def reply(payload):
        # Track how many tasks each employee runs at once (employee name is in the system prompt).
        who = payload["messages"][0]["content"].split("ROLE: ")[1].split(" in the ")[0]
        with lock:
            running[who] = running.get(who, 0) + 1
            peak[who] = max(peak.get(who, 0), running[who])
        time.sleep(0.01)
        with lock:
            running[who] -= 1
        return {"message": {"content": json.dumps({"action": "final", "output": "ok", "summary": "ok"})},
                "prompt_eval_count": 10, "eval_count": 5}

    llm.default = (200, reply)
    company = CompanySettings.get()
    company.max_concurrent_tasks = N_WORKERS * 2
    company.save()
    dept = Department.objects.create(name="Load", slug="load")
    model = make_model(make_provider("Load Ollama", llm.url))
    emps = []
    for i in range(N_EMPLOYEES):
        e = EmployeeProfile.objects.create(name=f"Agent{i}", job_title=f"Title{i}", department=dept, max_concurrent_tasks=1)
        EmployeeModelPolicy.objects.create(employee=e, model=model, priority=1)
        emps.append(e)
    tasks = [Task.objects.create(title=f"Load {i}", description="x", department=dept, assigned_employee=emps[i % N_EMPLOYEES],
                                 max_retries=0, max_steps=2) for i in range(N_TASKS)]
    for t in tasks:
        services.queue_task(t)

    errors = []

    def worker(n):
        wid = f"load-worker-{n}"
        try:
            idle = 0
            while idle < 3:
                claimed = engine.tick(wid)
                idle = idle + 1 if not claimed else 0
                if not claimed:
                    time.sleep(0.05)
        except Exception as exc:  # surface thread failures in the main thread
            errors.append(repr(exc))
        finally:
            close_old_connections()
            connection.close()

    started = time.monotonic()
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(N_WORKERS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=240)
    elapsed = time.monotonic() - started
    assert not errors, errors
    statuses = list(Task.objects.values_list("status", flat=True))
    assert statuses.count(Task.UNDER_REVIEW) == N_TASKS, {s: statuses.count(s) for s in set(statuses)}
    assert TaskExecution.objects.count() == N_TASKS  # exactly one execution per task: no duplicates
    assert TaskStatusChange.objects.filter(to_status=Task.RUNNING).count() == N_TASKS
    assert len({e.worker_id for e in TaskExecution.objects.all()}) > 1  # work was actually shared across workers
    assert max(peak.values()) == 1  # per-employee concurrency limit held under contention
    assert verify_chain() == (True, None)  # audit chain consistent under concurrent appends
    print(f"\n{N_TASKS} tasks on {N_WORKERS} workers ({connection.vendor}): {elapsed:.1f}s, {N_TASKS / elapsed:.1f} tasks/s")
