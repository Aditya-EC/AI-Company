import hashlib
import json

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone


class User(AbstractUser):
    """Human (or authorized AI) account. Authority comes from account_type + PermissionAssignment."""

    FOUNDER = "founder"
    CEO = "ceo"
    STAFF = "staff"
    ACCOUNT_TYPES = [(FOUNDER, "Founder"), (CEO, "CEO"), (STAFF, "Staff")]

    account_type = models.CharField(max_length=16, choices=ACCOUNT_TYPES, default=STAFF, db_index=True)
    department = models.ForeignKey("core.Department", null=True, blank=True, on_delete=models.SET_NULL, related_name="human_members")

    @property
    def is_founder(self):
        return self.account_type == self.FOUNDER and self.is_active

    @property
    def is_ceo(self):
        return self.account_type == self.CEO and self.is_active


class CompanySettings(models.Model):
    """Singleton company configuration owned by the Founder."""

    name = models.CharField(max_length=200, default="Virtual Company")
    mission = models.TextField(blank=True)
    strategy = models.TextField(blank=True, help_text="Goals, target markets, constraints.")
    development_standards = models.TextField(blank=True)
    permitted_software_categories = models.JSONField(default=list, blank=True, help_text="Empty = all template categories.")
    permitted_languages = models.JSONField(default=list, blank=True)
    permitted_frameworks = models.JSONField(default=list, blank=True)
    licensing_policy = models.TextField(blank=True, help_text="e.g. permitted dependency licenses.")
    testing_rules = models.TextField(blank=True)
    security_requirements = models.TextField(blank=True)
    deployment_restrictions = models.TextField(blank=True)
    repository_conventions = models.TextField(blank=True)
    ceo_approval_limit_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                                 help_text="CEO may approve proposals up to this estimated cost; above needs the Founder.")
    web_allowed_domains = models.JSONField(default=list, blank=True, help_text="If set, web.fetch is limited to these domains.")
    emergency_stop = models.BooleanField(default=False, help_text="Halts all new agent execution and tool use.")
    emergency_stop_reason = models.CharField(max_length=500, blank=True)
    paid_models_enabled = models.BooleanField(default=False, help_text="Paid API usage is disabled until the Founder enables it.")
    max_concurrent_tasks = models.PositiveIntegerField(default=2)
    default_max_retries = models.PositiveIntegerField(default=2)
    default_max_runtime_seconds = models.PositiveIntegerField(default=600)
    default_max_steps = models.PositiveIntegerField(default=8)
    ceo_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    ceo_employee = models.ForeignKey("core.EmployeeProfile", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    demo_mode = models.BooleanField(default=False, help_text="When on, sample data is clearly labeled as demo data.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "company settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Department(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    instructions = models.TextField(blank=True, help_text="Department-specific instructions given to its AI employees.")
    manager = models.ForeignKey("core.EmployeeProfile", null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_departments")
    is_active = models.BooleanField(default=True)
    is_paused = models.BooleanField(default=False)
    # 3D office placement (grid cell); editable by Founder.
    office_floor = models.PositiveIntegerField(default=0)
    office_x = models.IntegerField(default=0)
    office_z = models.IntegerField(default=0)
    color = models.CharField(max_length=7, default="#4f7cff")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class RoleTemplate(models.Model):
    """Reusable role definition stored in the database (not hardcoded in the engine)."""

    name = models.CharField(max_length=120, unique=True)
    category = models.CharField(max_length=60, blank=True)
    job_title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    system_instructions = models.TextField(blank=True)
    skills = models.JSONField(default=list, blank=True)
    default_tools = models.JSONField(default=list, blank=True, help_text="Tool slugs suggested for this role (not auto-granted).")
    required_capabilities = models.JSONField(default=list, blank=True, help_text="Model capabilities this role needs, e.g. ['json'].")
    is_builtin = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return self.name


class EmployeeProfile(models.Model):
    """An AI employee: a configuration executed by the shared orchestration engine."""

    ACTIVE, PAUSED, SUSPENDED, OFFLINE = "active", "paused", "suspended", "offline"
    STATUSES = [(ACTIVE, "Active"), (PAUSED, "Paused"), (SUSPENDED, "Suspended"), (OFFLINE, "Offline")]

    employee_id = models.CharField(max_length=20, unique=True, editable=False)
    name = models.CharField(max_length=120)
    avatar_color = models.CharField(max_length=7, default="#22c55e")
    job_title = models.CharField(max_length=120)
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="employees")
    role_template = models.ForeignKey(RoleTemplate, null=True, blank=True, on_delete=models.SET_NULL, related_name="employees")
    role_description = models.TextField(blank=True)
    system_instructions = models.TextField(blank=True)
    skills = models.JSONField(default=list, blank=True)
    manager = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reports")
    status = models.CharField(max_length=16, choices=STATUSES, default=ACTIVE, db_index=True)
    max_concurrent_tasks = models.PositiveIntegerField(default=1)
    budget_limit_usd = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    is_ai = models.BooleanField(default=True)
    user_account = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="employee_profile")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["department__name", "name"]
        indexes = [models.Index(fields=["department", "status"])]

    def __str__(self):
        return f"{self.name} ({self.job_title})"

    def save(self, *args, **kwargs):
        if not self.employee_id:
            with transaction.atomic():
                last = EmployeeProfile.objects.select_for_update().order_by("-id").values_list("id", flat=True).first() or 0
                self.employee_id = f"EMP-{last + 1:05d}"
        super().save(*args, **kwargs)

    @property
    def can_work(self):
        return self.status == self.ACTIVE and self.department.is_active and not self.department.is_paused


class PermissionAssignment(models.Model):
    """Grants a permission codename to a user, optionally scoped to a department or project."""

    PERMISSIONS = [
        ("view_company", "View company dashboards"),
        ("view_department", "View department"),
        ("manage_department", "Manage department"),
        ("view_employees", "View employees"),
        ("manage_employees", "Create / edit employees"),
        ("view_tasks", "View tasks"),
        ("view_projects", "View projects & proposals"),
        ("manage_projects", "Manage projects"),
        ("manage_tasks", "Create / control tasks"),
        ("review_tasks", "Review deliverables"),
        ("view_reports", "View reports"),
        ("view_monitor", "View live monitor"),
        ("view_models", "View model configuration"),
        ("view_audit", "View audit log"),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="permission_assignments")
    permission = models.CharField(max_length=40, choices=PERMISSIONS)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.CASCADE)
    project = models.ForeignKey("work.Project", null=True, blank=True, on_delete=models.CASCADE)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "permission", "department", "project"], name="uniq_perm_assignment")]
        indexes = [models.Index(fields=["user", "permission"])]


class Budget(models.Model):
    COMPANY, DEPARTMENT, EMPLOYEE, PROJECT, PROVIDER = "company", "department", "employee", "project", "provider"
    SCOPES = [(COMPANY, "Company"), (DEPARTMENT, "Department"), (EMPLOYEE, "Employee"), (PROJECT, "Project"), (PROVIDER, "Provider")]
    DAILY, MONTHLY, TOTAL = "daily", "monthly", "total"
    PERIODS = [(DAILY, "Daily"), (MONTHLY, "Monthly"), (TOTAL, "Total")]

    scope = models.CharField(max_length=16, choices=SCOPES)
    period = models.CharField(max_length=10, choices=PERIODS, default=MONTHLY)
    limit_usd = models.DecimalField(max_digits=12, decimal_places=4)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.CASCADE)
    employee = models.ForeignKey(EmployeeProfile, null=True, blank=True, on_delete=models.CASCADE)
    project = models.ForeignKey("work.Project", null=True, blank=True, on_delete=models.CASCADE)
    provider = models.ForeignKey("gateway.ModelProvider", null=True, blank=True, on_delete=models.CASCADE)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        targets = {"department": self.department_id, "employee": self.employee_id, "project": self.project_id, "provider": self.provider_id}
        if self.scope != self.COMPANY and not targets.get(self.scope):
            raise ValidationError(f"A {self.scope} budget needs a {self.scope}.")

    def __str__(self):
        return f"{self.get_scope_display()} {self.get_period_display()} ${self.limit_usd}"


class AuditEventQuerySet(models.QuerySet):
    def delete(self):
        raise PermissionError("Audit events are append-only.")

    def update(self, **kwargs):
        raise PermissionError("Audit events are append-only.")


class AuditEvent(models.Model):
    """Tamper-evident, hash-chained, append-only audit record."""

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    actor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_employee = models.ForeignKey(EmployeeProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_label = models.CharField(max_length=200)  # preserved even if the actor is deleted
    action = models.CharField(max_length=80, db_index=True)
    target_type = models.CharField(max_length=60, blank=True, db_index=True)
    target_id = models.CharField(max_length=60, blank=True, db_index=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    prev_hash = models.CharField(max_length=64, unique=True)  # unique => the chain cannot fork
    hash = models.CharField(max_length=64, unique=True)

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        ordering = ["-id"]

    def compute_hash(self):
        payload = json.dumps(
            {
                "created_at": self.created_at.isoformat(),
                "actor": self.actor_label,
                "action": self.action,
                "target_type": self.target_type,
                "target_id": self.target_id,
                "details": self.details,
                "prev": self.prev_hash,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Audit events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Audit events are append-only.")


class Incident(models.Model):
    SEVERITIES = [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("critical", "Critical")]
    title = models.CharField(max_length=200)
    severity = models.CharField(max_length=10, choices=SEVERITIES, default="medium")
    category = models.CharField(max_length=40, default="operations")  # security, provider, operations
    description = models.TextField(blank=True)
    related_task = models.ForeignKey("work.Task", null=True, blank=True, on_delete=models.SET_NULL)
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=40)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=300, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
