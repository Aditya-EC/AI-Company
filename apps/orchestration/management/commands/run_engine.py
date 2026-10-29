"""Development/production worker without Celery: polls the database for queued work."""
import signal
import time
from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from apps.core.models import CompanySettings
from apps.orchestration import engine


class Command(BaseCommand):
    help = "Run the shared orchestration engine worker loop."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=2.0)
        parser.add_argument("--once", action="store_true", help="Run a single scheduling pass and exit.")

    def handle(self, *args, interval, once, **opts):
        worker = engine.new_worker_id()
        stopping = {"flag": False}

        def stop(*_):
            stopping["flag"] = True
            self.stdout.write("Stopping after current tasks reach a safe checkpoint...")

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.stdout.write(f"VCoOS engine worker {worker} started")
        max_workers = max(CompanySettings.get().max_concurrent_tasks, 1) + 1  # +1 slot for a meeting
        pool = ThreadPoolExecutor(max_workers=max_workers)
        futures = set()
        while not stopping["flag"]:
            close_old_connections()
            futures = {f for f in futures if not f.done()}
            claimed = engine.tick(worker, run=False)
            engine.heartbeat(worker, claimed=len(claimed))
            for tid in claimed:
                self.stdout.write(f"claimed task {tid}")
                futures.add(pool.submit(self._run, tid, worker))
            if not CompanySettings.get().emergency_stop and len(futures) < max_workers:
                for meeting in engine.claim_meetings(worker):
                    self.stdout.write(f"claimed meeting {meeting.pk}")
                    futures.add(pool.submit(self._run_meeting, meeting, worker))
            if once:
                break
            time.sleep(interval)
        pool.shutdown(wait=True)
        engine.heartbeat(worker, stopped=True)

    @staticmethod
    def _run_meeting(meeting, worker):
        try:
            engine.run_meeting(meeting, worker)
        finally:
            close_old_connections()

    @staticmethod
    def _run(task_id, worker):
        try:
            engine.execute_task(task_id, worker)
        finally:
            close_old_connections()
