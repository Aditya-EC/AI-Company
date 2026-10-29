from datetime import timedelta

from django.db import models
from django.utils import timezone


class WorkerHeartbeat(models.Model):
    """Liveness record written by every engine worker (run_engine or Celery) on each scheduling pass."""

    worker_id = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=10, default="engine")  # engine / celery
    hostname = models.CharField(max_length=200)
    pid = models.PositiveIntegerField()
    started_at = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)
    tasks_claimed = models.PositiveIntegerField(default=0)
    stopped = models.BooleanField(default=False)

    class Meta:
        ordering = ["-last_seen"]

    ALIVE_SECONDS = 60

    @classmethod
    def alive(cls):
        return cls.objects.filter(stopped=False, last_seen__gte=timezone.now() - timedelta(seconds=cls.ALIVE_SECONDS))
