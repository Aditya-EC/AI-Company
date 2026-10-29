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
    DRAFT, SUBMITTED, APPROVED, REJECTED, DEFERRED, REVISION, ESCALATED = (
        "draft", "submitted", "approved", "rejected", "deferred", "revision", "escalated")
    STATUSES = [(DRAFT, "Draft"), (SUBMITTED, "Submitted"), (APPROVED, "Approved"), (REJECTED, "Rejected"),
                (DEFERRED, "Deferred"), (REVISION, "Revision requested"), (ESCALATED, "Escalated to Founder")]
    title = models.CharField(max_length=200)
    software_category = models.CharField(max_length=60, blank=True)
    problem_statement = models.TextField()
    target_users = models.TextField(blank=True)
    market_research = models.TextField(blank=True, help_text="Market and competitor research summary.")
    evidence = models.JSONField(default=list, blank=True,
                                help_text="[{claim, source_url, retrieved}] - retrieved is set by the system, never by the agent.")
    proposed_features = models.JSONField(default=list, blank=True)
    technical_feasibility = models.TextField(blank=True)
    recommended_stack = models.JSONField(default=list, blank=True)
    estimated_effort_days = models.DecimalField(max_digits=8, decimal_places=1, null=True, blank=True)
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    risks = models.JSONField(default=list, blank=True)
    revenue_hypothesis = models.TextField(blank=True)
    validation_plan = models.TextField(blank=True)
    confidence = models.PositiveSmallIntegerField(null=True, blank=True, help_text="0-100, as stated by the author.")
    assumptions = models.JSONField(default=list, blank=True)
    uncertainties = models.JSONField(default=list, blank=True)
    department = models.ForeignKey("core.Department", null=True, blank=True, on_delete=models.SET_NULL, related_name="proposals")
    proposed_by = models.ForeignKey("core.EmployeeProfile", null=True, blank=True, on_delete=models.SET_NULL)
    submitted_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    source_task = models.ForeignKey("work.Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="produced_proposals")
    revision_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="revisions")
    status = models.CharField(max_length=12, choices=STATUSES, default=DRAFT, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def code(self):
        return f"P-{self.pk}"


class ProjectTemplate(models.Model):
    """Software Factory template per category; guides planning (default milestones, stack)."""

    category = models.CharField(max_length=60, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    default_milestones = models.JSONField(default=list, blank=True)
    default_stack = models.JSONField(default=list, blank=True)
    uses_repository = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Project(models.Model):
    PLANNING, ACTIVE, PAUSED, COMPLETED, CANCELLED = "planning", "active", "paused", "completed", "cancelled"
    STATUSES = [(PLANNING, "Planning"), (ACTIVE, "Active"), (PAUSED, "Paused"), (COMPLETED, "Completed"), (CANCELLED, "Cancelled")]
    PLAN_NONE, PLAN_IN_PROGRESS, PLAN_PROPOSED, PLAN_ACTIVE = "none", "in_progress", "proposed", "active"
    PLAN_STATUSES = [(PLAN_NONE, "No plan"), (PLAN_IN_PROGRESS, "Planning in progress"), (PLAN_PROPOSED, "Plan awaiting activation"),
                     (PLAN_ACTIVE, "Plan active")]
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    proposal = models.OneToOneField(ProjectProposal, null=True, blank=True, on_delete=models.SET_NULL, related_name="project")
    template = models.ForeignKey(ProjectTemplate, null=True, blank=True, on_delete=models.SET_NULL)
    category = models.CharField(max_length=60, blank=True)
    department = models.ForeignKey("core.Department", null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
    priority = models.IntegerField(choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=12, choices=STATUSES, default=ACTIVE, db_index=True)
    plan_status = models.CharField(max_length=12, choices=PLAN_STATUSES, default=PLAN_NONE)
    has_repository = models.BooleanField(default=False)
    budget_usd = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    deadline = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "name"]

    def __str__(self):
        return self.name

    @property
    def code(self):
        return f"PRJ-{self.pk}"


class Decision(models.Model):
    """A recorded management decision (proposal reviews, overrides, priority changes, plan activation, releases)."""

    KINDS = [("proposal", "Proposal decision"), ("override", "Founder override"), ("priority", "Priority change"),
             ("plan", "Plan activation"), ("project_status", "Project status"), ("release", "Release decision")]
    kind = models.CharField(max_length=20, choices=KINDS)
    outcome = models.CharField(max_length=40)
    rationale = models.TextField(blank=True)
    effects = models.JSONField(default=dict, blank=True)
    proposal = models.ForeignKey(ProjectProposal, null=True, blank=True, on_delete=models.CASCADE, related_name="decisions")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="decisions")
    made_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    authority = models.CharField(max_length=10)  # founder / ceo
    overrides = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="overridden_by")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]


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
    required_capabilities = models.JSONField(default=list, blank=True, help_text="Model capabilities, e.g. ['json'].")
    required_skills = models.JSONField(default=list, blank=True, help_text="Employee skills used for automatic assignment.")
    DELIVERABLE, PROPOSAL, PLAN = "deliverable", "proposal", "plan"
    OUTPUT_KINDS = [(DELIVERABLE, "Deliverable"), (PROPOSAL, "Project proposal"), (PLAN, "Project plan")]
    output_kind = models.CharField(max_length=12, choices=OUTPUT_KINDS, default=DELIVERABLE)
    branch_name = models.CharField(max_length=80, blank=True, help_text="Git branch in the project repository.")
    # Limits
    max_retries = models.PositiveIntegerField(default=2)
    retry_count = models.PositiveIntegerField(default=0)
    max_runtime_seconds = models.PositiveIntegerField(default=600)
    max_steps = models.PositiveIntegerField(default=8)
    max_cost_usd = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    actual_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    deadline = models.DateTimeField(null=True, blank=True)
    deadline_warned_at = models.DateTimeField(null=True, blank=True)
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


class TestRun(models.Model):
    """Actual test execution evidence. Results come from the test runner's exit code and output, never from an agent's claim."""

    __test__ = False  # not a pytest class
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="test_runs")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="test_runs")
    execution = models.ForeignKey(TaskExecution, null=True, blank=True, on_delete=models.SET_NULL)
    ref = models.CharField(max_length=80, help_text="Branch or 'main'.")
    commit_sha = models.CharField(max_length=40, blank=True)
    dirty = models.BooleanField(default=False, help_text="Working tree had uncommitted changes.")
    command = models.CharField(max_length=200)
    exit_code = models.IntegerField()
    tests_run = models.PositiveIntegerField(null=True, blank=True)
    failures = models.PositiveIntegerField(null=True, blank=True)
    passed = models.BooleanField()
    timed_out = models.BooleanField(default=False)
    output = models.TextField(blank=True)
    triggered_by = models.CharField(max_length=200)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]


class Release(models.Model):
    DRAFT, AWAITING_APPROVAL, APPROVED, REJECTED = "draft", "awaiting_approval", "approved", "rejected"
    STATUSES = [(DRAFT, "Draft"), (AWAITING_APPROVAL, "Awaiting Founder approval"), (APPROVED, "Approved for deployment"),
                (REJECTED, "Rejected")]
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="releases")
    version = models.CharField(max_length=40)
    status = models.CharField(max_length=20, choices=STATUSES, default=DRAFT)
    release_notes = models.TextField()
    known_issues = models.TextField(blank=True)
    deployment_plan = models.TextField(blank=True)
    commit_sha = models.CharField(max_length=40, blank=True)
    test_run = models.ForeignKey(TestRun, null=True, blank=True, on_delete=models.SET_NULL)
    checklist = models.JSONField(default=dict, blank=True, help_text="Readiness evidence snapshot at submission time.")
    approval = models.OneToOneField(ApprovalRequest, null=True, blank=True, on_delete=models.SET_NULL, related_name="release")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["project", "version"], name="uniq_project_version")]
