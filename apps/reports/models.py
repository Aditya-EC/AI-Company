from django.conf import settings
from django.db import models


class ReportJob(models.Model):
    """A generated report export. Files are served only through the permission-checked download view."""

    KINDS = [("company", "Company"), ("department", "Department"), ("employee", "Employee"), ("financial", "Financial & AI usage")]
    FORMATS = [("csv", "CSV"), ("pdf", "PDF")]
    kind = models.CharField(max_length=12, choices=KINDS)
    fmt = models.CharField(max_length=4, choices=FORMATS)
    params = models.JSONField(default=dict)
    status = models.CharField(max_length=10, default="pending", choices=[("pending", "Pending"), ("done", "Done"), ("failed", "Failed")])
    file_name = models.CharField(max_length=200, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="report_jobs")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
