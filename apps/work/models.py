from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class Priority(models.IntegerChoices):
    CRITICAL = 0, "Critical"
    HIGH = 1, "High"
    NORMAL = 2, "Normal"
    LOW = 3, "Low"
    ON_HOLD = 9, "On hold"


class ProjectProposal(models.Model):
    STATUSES = [("draft", "Draft"), ("submitted", "Submitted"), ("approved", "Approved"), ("rejected", "Rejected"),
                ("deferred", "Deferred"), ("revision", "Revision requested")]
    title = models.CharField(max_length=200)
    problem_statement = models.TextField(blank=True)
    content = models.JSONField(default=dict, blank=True, help_text="Structured proposal: users, research, features, risks...")
    proposed_by = models.ForeignKey("core.EmployeeProfile", null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=12, choices=STATUSES, default="draft", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Project(models.Model):
    STATUSES = [("planning", "Planning"), ("active", "Active"), ("paused", "Paused"), ("completed", "Completed"), ("cancelled", "Cancelled")]
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    proposal = models.OneToOneField(ProjectProposal, null=True, blank=True, on_delete=models.SET_NULL)
    department = models.ForeignKey("core.Department", null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
    priority = models.IntegerField(choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=12, choices=STATUSES, default="active", db_index=True)
    budget_usd = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    deadline = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "name"]

    def __str__(self):
        return self.name


class Milestone(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="milestones")
    name = models.CharField(max_length=200)
    due_date = models.DateField(null=True, blank=True)
    is_done = models.BooleanField(default=False)


class Task(models.Model):
    DRAFT, PROPOSED, APPROVED, QUEUED, ASSIGNED, RUNNING = "draft", "proposed", "approved", "queued", "assigned", "running"
    WAITING, BLOCKED, UNDER_REVIEW, REVISION, COMPLETED = "waiting_input", "blocked", "under_review", "revision_required", "completed"
    FAILED, CANCELLED, PAUSED, AWAITING_APPROVAL = "failed", "cancelled", "paused", "awaiting_approval"
    STATUSES = [
        (DRAFT, "Draft"), (PROPOSED, "Proposed"), (APPROVED, "Approved"), (QUEUED, "Queued"), (ASSIGNED, "Assigned"),
        (RUNNING, "Running"), (WAITING, "Waiting for input"), (BLOCKED, "Blocked"), (AWAITING_APPROVAL, "Awaiting approval"),
        (UNDER_REVIEW, "Under review"), (REVISION, "Revision required"), (COMPLETED, "Completed"), (FAILED, "Failed"),
        (CANCELLED, "Cancelled"), (PAUSED, "Paused"),
    ]
    TERMINAL = {COMPLETED, FAILED, CANCELLED}

    title = models.CharField(max_length=200)
    description = models.TextField()
    acceptance_criteria = models.TextField(blank=True)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    milestone = models.ForeignKey(Milestone, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    department = models.ForeignKey("core.Department", on_delete=models.PROTECT, related_name="tasks")
    assigned_employee = models.ForeignKey("core.EmployeeProfile", null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    parent_task = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="revisions")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="created_tasks")
    priority = models.IntegerField(choices=Priority.choices, default=Priority.NORMAL, db_index=True)
    status = models.CharField(max_length=20, choices=STATUSES, default=DRAFT, db_index=True)
    status_reason = models.CharField(max_length=500, blank=True)
    # Governance
    requires_review = models.BooleanField(default=True)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="review_tasks")
    requires_approval = models.BooleanField(default=False, help_text="Execution needs an approved ApprovalRequest first.")
    is_confidential = models.BooleanField(default=False)
    is_idempotent = models.BooleanField(default=True, help_text="Safe to re-run automatically after an interruption.")
    required_capabilities = models.JSONField(default=list, blank=True)
    # Limits
    max_retries = models.PositiveIntegerField(default=2)
    retry_count = models.PositiveIntegerField(default=0)
    max_runtime_seconds = models.PositiveIntegerField(default=600)
    max_steps = models.PositiveIntegerField(default=8)
    max_cost_usd = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    actual_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    deadline = models.DateTimeField(null=True, blank=True)
    # Results
    output = models.TextField(blank=True)
    error = models.TextField(blank=True)
    # Engine bookkeeping (persistent, so work survives restarts)
    lease_owner = models.CharField(max_length=100, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    cancel_requested = models.BooleanField(default=False)
    pause_requested = models.BooleanField(default=False)
    progress_note = models.CharField(max_length=300, blank=True, help_text="Last verifiable progress step.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["priority", "created_at"]
        indexes = [
            models.Index(fields=["status", "priority", "queued_at"]),
            models.Index(fields=["assigned_employee", "status"]),
            models.Index(fields=["lease_expires_at"]),
        ]

    def __str__(self):
        return f"T-{self.pk} {self.title}"

    @property
    def code(self):
        return f"T-{self.pk}"


class TaskDependency(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="dependencies")
    depends_on = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="dependents")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "depends_on"], name="uniq_task_dependency"),
            models.CheckConstraint(condition=~models.Q(task=models.F("depends_on")), name="no_self_dependency"),
        ]


class TaskStatusChange(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="status_history")
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    actor_label = models.CharField(max_length=200)
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]


class TaskExecution(models.Model):
    RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, PAUSED = "running", "succeeded", "failed", "cancelled", "interrupted", "paused"
    STATUSES = [(RUNNING, "Running"), (SUCCEEDED, "Succeeded"), (FAILED, "Failed"), (CANCELLED, "Cancelled"),
                (INTERRUPTED, "Interrupted"), (PAUSED, "Paused")]
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="executions")
    employee = models.ForeignKey("core.EmployeeProfile", null=True, on_delete=models.SET_NULL)
    attempt_number = models.PositiveIntegerField()
    idempotency_key = models.CharField(max_length=80, unique=True)
    worker_id = models.CharField(max_length=100)
    status = models.CharField(max_length=12, choices=STATUSES, default=RUNNING)
    model_used = models.ForeignKey("gateway.AIModel", null=True, blank=True, on_delete=models.SET_NULL)
    routing_log = models.JSONField(default=list, blank=True)
    steps = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [models.UniqueConstraint(fields=["task", "attempt_number"], name="uniq_task_attempt")]


class ToolCall(models.Model):
    execution = models.ForeignKey(TaskExecution, on_delete=models.CASCADE, related_name="tool_calls")
    tool_slug = models.CharField(max_length=80)
    input_data = models.JSONField(default=dict)
    output_summary = models.TextField(blank=True)
    allowed = models.BooleanField()
    denial_reason = models.CharField(max_length=300, blank=True)
    success = models.BooleanField(default=False)
    duration_ms = models.PositiveIntegerField(default=0)
    idempotency_key = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]


class Artifact(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="artifacts")
    execution = models.ForeignKey(TaskExecution, null=True, on_delete=models.SET_NULL, related_name="artifacts")
    employee = models.ForeignKey("core.EmployeeProfile", null=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=30, default="text")  # text, file, report, code
    content = models.TextField(blank=True)
    file_path = models.CharField(max_length=500, blank=True, help_text="Path relative to the task workspace.")
    sha256 = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Review(models.Model):
    ACCEPT, REJECT = "accept", "reject"
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="reviews")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    outcome = models.CharField(max_length=10, choices=[(ACCEPT, "Accepted"), (REJECT, "Rejected")])
    comments = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ApprovalRequest(models.Model):
    PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
    KINDS = [("task_execution", "Task execution"), ("budget_exceed", "Exceed budget"), ("paid_provider", "Enable paid provider"),
             ("deployment", "Production deployment"), ("external_comm", "External communication"), ("data_deletion", "Delete critical data"),
             ("security_policy", "Change security policy"), ("other", "Other")]
    kind = models.CharField(max_length=24, choices=KINDS)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    required_authority = models.CharField(max_length=10, choices=[("founder", "Founder"), ("ceo", "CEO or Founder")], default="ceo")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.CASCADE, related_name="approvals")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="approvals")
    requested_by_label = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=[(PENDING, "Pending"), (APPROVED, "Approved"), (REJECTED, "Rejected")], default=PENDING, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
