from decimal import Decimal

from django.db import models
from django.utils import timezone

from .crypto import decrypt, encrypt


class ModelProvider(models.Model):
    OLLAMA, OPENAI, ANTHROPIC, GEMINI, XAI, NVIDIA, OPENAI_COMPAT = (
        "ollama", "openai", "anthropic", "gemini", "xai", "nvidia", "openai_compatible",
    )
    KINDS = [
        (OLLAMA, "Ollama (local)"),
        (OPENAI, "OpenAI"),
        (ANTHROPIC, "Anthropic Claude"),
        (GEMINI, "Google Gemini (OpenAI-compatible endpoint)"),
        (XAI, "xAI Grok"),
        (NVIDIA, "NVIDIA AI inference"),
        (OPENAI_COMPAT, "Other OpenAI-compatible API"),
    ]
    DEFAULT_URLS = {
        OLLAMA: "http://localhost:11434",
        OPENAI: "https://api.openai.com/v1",
        ANTHROPIC: "https://api.anthropic.com",
        GEMINI: "https://generativelanguage.googleapis.com/v1beta/openai",
        XAI: "https://api.x.ai/v1",
        NVIDIA: "https://integrate.api.nvidia.com/v1",
    }
    HEALTH = [("unknown", "Unknown"), ("healthy", "Healthy"), ("degraded", "Degraded"), ("down", "Down"), ("auth_error", "Invalid credentials")]

    name = models.CharField(max_length=120, unique=True)
    kind = models.CharField(max_length=24, choices=KINDS)
    base_url = models.URLField(max_length=300)
    credential_ref = models.CharField(max_length=120, blank=True, help_text="Reference like env:OPENAI_API_KEY. Preferred over storing keys.")
    encrypted_api_key = models.TextField(blank=True, editable=False)
    is_enabled = models.BooleanField(default=True)
    is_local = models.BooleanField(default=False)
    is_paid = models.BooleanField(default=False, help_text="Calls may incur charges. Requires Founder enablement + budget.")
    privacy_notes = models.TextField(blank=True)
    allow_confidential_data = models.BooleanField(default=False, help_text="May receive confidential company data.")
    rate_limit_rpm = models.PositiveIntegerField(null=True, blank=True)
    timeout_seconds = models.PositiveIntegerField(default=120)
    # Health & circuit breaker
    health_status = models.CharField(max_length=16, choices=HEALTH, default="unknown")
    last_health_check = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    circuit_open_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def set_api_key(self, raw):
        self.encrypted_api_key = encrypt(raw) if raw else ""

    def get_api_key(self):
        """Resolve the credential server-side only. Never send this to templates or logs."""
        import os

        if self.credential_ref.startswith("env:"):
            return os.environ.get(self.credential_ref[4:], "")
        if self.encrypted_api_key:
            return decrypt(self.encrypted_api_key)
        return ""

    @property
    def has_credential(self):
        return bool(self.encrypted_api_key or self.credential_ref)

    @property
    def circuit_open(self):
        return bool(self.circuit_open_until and self.circuit_open_until > timezone.now())


class AIModel(models.Model):
    provider = models.ForeignKey(ModelProvider, on_delete=models.CASCADE, related_name="models")
    model_identifier = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200)
    context_window = models.PositiveIntegerField(null=True, blank=True)
    max_output_tokens = models.PositiveIntegerField(default=2048)
    supports_tools = models.BooleanField(default=False)
    supports_json = models.BooleanField(default=False, help_text="Structured/JSON output support.")
    supports_streaming = models.BooleanField(default=False)
    supports_vision = models.BooleanField(default=False)
    capabilities_verified_at = models.DateTimeField(null=True, blank=True)
    price_input_per_mtok = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    price_output_per_mtok = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    daily_request_quota = models.PositiveIntegerField(null=True, blank=True, help_text="Known free-tier/request quota; editable.")
    is_enabled = models.BooleanField(default=True)
    is_approved = models.BooleanField(default=False, help_text="Founder-approved for employee use.")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["provider__name", "display_name"]
        constraints = [models.UniqueConstraint(fields=["provider", "model_identifier"], name="uniq_provider_model")]

    def __str__(self):
        return f"{self.display_name} @ {self.provider.name}"

    @property
    def is_paid(self):
        return self.provider.is_paid or self.price_input_per_mtok > 0 or self.price_output_per_mtok > 0

    def capabilities(self):
        caps = {"text"}
        if self.supports_tools:
            caps.add("tools")
        if self.supports_json:
            caps.add("json")
        if self.supports_streaming:
            caps.add("streaming")
        if self.supports_vision:
            caps.add("vision")
        return caps

    def estimate_cost(self, input_tokens, output_tokens):
        return (Decimal(input_tokens) * self.price_input_per_mtok + Decimal(output_tokens) * self.price_output_per_mtok) / Decimal(1_000_000)


class EmployeeModelPolicy(models.Model):
    """Ordered model priority list per employee (1 = primary)."""

    employee = models.ForeignKey("core.EmployeeProfile", on_delete=models.CASCADE, related_name="model_policies")
    model = models.ForeignKey(AIModel, on_delete=models.CASCADE, related_name="employee_policies")
    priority = models.PositiveIntegerField()
    allow_paid = models.BooleanField(default=False, help_text="Explicitly permit this paid model for this employee.")

    class Meta:
        ordering = ["employee", "priority"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "priority"], name="uniq_employee_priority"),
            models.UniqueConstraint(fields=["employee", "model"], name="uniq_employee_model"),
        ]


class UsageRecord(models.Model):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    employee = models.ForeignKey("core.EmployeeProfile", null=True, on_delete=models.SET_NULL, related_name="usage")
    task = models.ForeignKey("work.Task", null=True, on_delete=models.SET_NULL, related_name="usage")
    execution = models.ForeignKey("work.TaskExecution", null=True, on_delete=models.SET_NULL, related_name="usage")
    department = models.ForeignKey("core.Department", null=True, on_delete=models.SET_NULL)
    project = models.ForeignKey("work.Project", null=True, on_delete=models.SET_NULL)
    provider = models.ForeignKey(ModelProvider, null=True, on_delete=models.SET_NULL)
    model = models.ForeignKey(AIModel, null=True, on_delete=models.SET_NULL)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    tokens_reported = models.BooleanField(default=False, help_text="True if the provider reported token counts.")
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    billed_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True, help_text="Only from provider billing data.")
    latency_ms = models.PositiveIntegerField(default=0)
    success = models.BooleanField(default=True)
    error_kind = models.CharField(max_length=40, blank=True)
    was_fallback = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["employee", "created_at"]), models.Index(fields=["provider", "created_at"])]
