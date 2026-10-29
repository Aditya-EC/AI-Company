from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ToolDefinition(models.Model):
    RISK = [("low", "Low"), ("medium", "Medium"), ("high", "High")]
    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField()
    input_schema = models.JSONField(default=dict)
    output_schema = models.JSONField(default=dict)
    risk_level = models.CharField(max_length=8, choices=RISK, default="low")
    timeout_seconds = models.PositiveIntegerField(default=30)
    resource_limits = models.JSONField(default=dict, blank=True)
    error_behavior = models.TextField(blank=True)
    is_enabled = models.BooleanField(default=True)
    has_handler = models.BooleanField(default=False, editable=False, help_text="Implemented in this installation.")

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.slug


class ToolPermission(models.Model):
    """Least-privilege grant (or explicit deny) of a tool at one scope. Deny wins."""

    COMPANY, DEPARTMENT, EMPLOYEE, PROJECT, TASK = "company", "department", "employee", "project", "task"
    SCOPES = [(COMPANY, "Company"), (DEPARTMENT, "Department"), (EMPLOYEE, "Employee"), (PROJECT, "Project"), (TASK, "Task")]
    tool = models.ForeignKey(ToolDefinition, on_delete=models.CASCADE, related_name="permissions")
    scope = models.CharField(max_length=12, choices=SCOPES)
    department = models.ForeignKey("core.Department", null=True, blank=True, on_delete=models.CASCADE)
    employee = models.ForeignKey("core.EmployeeProfile", null=True, blank=True, on_delete=models.CASCADE, related_name="tool_permissions")
    project = models.ForeignKey("work.Project", null=True, blank=True, on_delete=models.CASCADE)
    task = models.ForeignKey("work.Task", null=True, blank=True, on_delete=models.CASCADE)
    allow = models.BooleanField(default=True)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["tool", "scope"])]

    def clean(self):
        target = {self.DEPARTMENT: self.department_id, self.EMPLOYEE: self.employee_id, self.PROJECT: self.project_id, self.TASK: self.task_id}
        if self.scope != self.COMPANY and not target.get(self.scope):
            raise ValidationError(f"{self.scope} scope requires a {self.scope}")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
