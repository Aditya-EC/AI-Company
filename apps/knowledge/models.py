from django.conf import settings
from django.db import models
from django.utils import timezone


class KnowledgeDocument(models.Model):
    """A versioned, access-controlled company document. Only approved versions are served to agents."""

    CATEGORIES = [
        ("policy", "Company policy"), ("standard", "Coding standard"), ("procedure", "Approved procedure"),
        ("requirements", "Product requirements"), ("technical", "Technical documentation"), ("adr", "Architecture decision"),
        ("research", "Research report"), ("project", "Project document"), ("meeting_notes", "Meeting notes"),
        ("lessons", "Lessons learned"),
    ]
    # Categories that act as binding company rules: approval is Founder-only.
    AUTHORITATIVE = {"policy", "standard", "procedure"}
    COMPANY, DEPARTMENT, PROJECT, LEADERSHIP = "company", "department", "project", "leadership"
    ACCESS = [(COMPANY, "Whole company"), (DEPARTMENT, "Department"), (PROJECT, "Project"), (LEADERSHIP, "Founder & CEO only")]
    DRAFT, IN_REVIEW, APPROVED, ARCHIVED = "draft", "in_review", "approved", "archived"
    STATUSES = [(DRAFT, "Draft"), (IN_REVIEW, "In review"), (APPROVED, "Approved"), (ARCHIVED, "Archived")]
    SOURCES = [("human", "Human-authored"), ("agent", "AI employee"), ("external", "External content"), ("system", "System record")]

    title = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=CATEGORIES)
    access = models.CharField(max_length=12, choices=ACCESS, default=COMPANY)
    department = models.ForeignKey("core.Department", null=True, blank=True, on_delete=models.SET_NULL, related_name="documents")
    project = models.ForeignKey("work.Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="documents")
    status = models.CharField(max_length=10, choices=STATUSES, default=DRAFT, db_index=True)
    source = models.CharField(max_length=10, choices=SOURCES, default="human")
    source_url = models.URLField(blank=True, max_length=500)
    source_task = models.ForeignKey("work.Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    current_version = models.ForeignKey("KnowledgeVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                        help_text="The approved version served to readers and agents.")
    tags = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]
        indexes = [models.Index(fields=["status", "access"])]

    def __str__(self):
        return self.title

    @property
    def is_authoritative(self):
        return self.category in self.AUTHORITATIVE and self.status == self.APPROVED and self.current_version_id is not None


class KnowledgeVersion(models.Model):
    document = models.ForeignKey(KnowledgeDocument, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    content = models.TextField()
    change_note = models.CharField(max_length=300, blank=True)
    author_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    author_employee = models.ForeignKey("core.EmployeeProfile", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    PENDING, APPROVED, REJECTED, SUPERSEDED = "pending", "approved", "rejected", "superseded"
    status = models.CharField(max_length=10, default=PENDING, choices=[(PENDING, "Pending review"), (APPROVED, "Approved"),
                                                                      (REJECTED, "Rejected"), (SUPERSEDED, "Superseded")])
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["document", "number"], name="uniq_doc_version")]


class Meeting(models.Model):
    KINDS = [("management", "CEO & department managers"), ("architecture", "Architecture review"), ("planning", "Project planning"),
             ("proposal", "Proposal evaluation"), ("code_review", "Code review discussion"), ("release", "Release readiness review"),
             ("other", "Other")]
    SCHEDULED, QUEUED, RUNNING, COMPLETED, BLOCKED, CANCELLED = "scheduled", "queued", "running", "completed", "blocked", "cancelled"
    STATUSES = [(SCHEDULED, "Scheduled"), (QUEUED, "Queued"), (RUNNING, "Running"), (COMPLETED, "Completed"),
                (BLOCKED, "Ended with blocker"), (CANCELLED, "Cancelled")]
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=KINDS, default="other")
    purpose = models.TextField(help_text="What decision or output this meeting must produce.")
    agenda = models.JSONField(default=list)
    participants = models.ManyToManyField("core.EmployeeProfile", related_name="meetings")
    facilitator = models.ForeignKey("core.EmployeeProfile", on_delete=models.PROTECT, related_name="facilitated_meetings")
    project = models.ForeignKey("work.Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="meetings")
    department = models.ForeignKey("core.Department", null=True, blank=True, on_delete=models.SET_NULL, related_name="meetings")
    max_rounds = models.PositiveSmallIntegerField(default=2)
    status = models.CharField(max_length=10, choices=STATUSES, default=SCHEDULED, db_index=True)
    status_reason = models.CharField(max_length=500, blank=True)
    lease_owner = models.CharField(max_length=100, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempt = models.PositiveSmallIntegerField(default=0)
    summary = models.TextField(blank=True)
    decisions = models.JSONField(default=list, blank=True)
    action_items = models.JSONField(default=list, blank=True)
    unresolved_questions = models.JSONField(default=list, blank=True)
    action_tasks = models.ManyToManyField("work.Task", blank=True, related_name="source_meetings")
    notes_document = models.ForeignKey(KnowledgeDocument, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    model_calls = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def code(self):
        return f"M-{self.pk}"


class MeetingTurn(models.Model):
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="turns")
    attempt = models.PositiveSmallIntegerField(default=1)
    round = models.PositiveSmallIntegerField()
    employee = models.ForeignKey("core.EmployeeProfile", null=True, on_delete=models.SET_NULL)
    role = models.CharField(max_length=12, default="participant")  # participant / facilitator
    content = models.TextField()
    ready_to_conclude = models.BooleanField(default=False)
    model = models.ForeignKey("gateway.AIModel", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["round", "created_at", "id"]
