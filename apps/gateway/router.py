"""Model router: per-employee ordered fallback with capability, privacy, cost, budget,
quota and health checks. Every selection and rejection is recorded."""
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from apps.core import audit, budget
from apps.core.models import CompanySettings, Incident
from apps.core.notify import notify_leadership

from .adapters import GatewayError, get_adapter
from .models import ModelProvider, UsageRecord

log = logging.getLogger("vcoos.gateway")

MAX_RETRIES_PER_MODEL = 2
BACKOFF_BASE = 1.0
BACKOFF_CAP = 20.0
CIRCUIT_THRESHOLD = 3
CIRCUIT_COOLDOWN = timedelta(seconds=90)


class NoModelAvailable(Exception):
    def __init__(self, attempts):
        self.attempts = attempts
        summary = "; ".join(f"{a['model']}: {a['outcome']}" for a in attempts) or "no models configured for this employee"
        super().__init__(f"No approved model available ({summary})")


@dataclass
class RouteResult:
    text: str
    model: object
    attempts: list = field(default_factory=list)
    usage: object = None


def _approx_tokens(messages):
    return sum(len(m.get("content", "")) for m in messages) // 4 + 1


def _mark_success(provider):
    ModelProvider.objects.filter(pk=provider.pk).update(
        consecutive_failures=0, health_status="healthy", last_error="", circuit_open_until=None, last_health_check=timezone.now()
    )


def _mark_failure(provider, err):
    updates = {"consecutive_failures": F("consecutive_failures") + 1, "last_error": err.message[:500], "last_health_check": timezone.now()}
    if err.kind == GatewayError.INVALID_CREDENTIALS:
        updates["health_status"] = "auth_error"
    elif err.kind in (GatewayError.TRANSIENT, GatewayError.TIMEOUT):
        updates["health_status"] = "degraded"
    ModelProvider.objects.filter(pk=provider.pk).update(**updates)
    provider.refresh_from_db()
    if provider.consecutive_failures >= CIRCUIT_THRESHOLD and err.kind in GatewayError.RETRYABLE:
        provider.circuit_open_until = timezone.now() + CIRCUIT_COOLDOWN
        provider.health_status = "down"
        provider.save(update_fields=["circuit_open_until", "health_status"])
        audit.record("provider.circuit_opened", target=provider, details={"error": err.kind})
    if err.kind == GatewayError.INVALID_CREDENTIALS:
        Incident.objects.create(title=f"Invalid credentials for provider {provider.name}", severity="high", category="provider")
        notify_leadership("provider_auth", f"Invalid credentials: {provider.name}", "Update the credential reference for this provider.")
    elif err.kind == GatewayError.QUOTA_EXHAUSTED:
        notify_leadership("quota", f"Quota exhausted: {provider.name}", err.message)


def eligibility(entry, requirements, company, *, employee, task, est_cost):
    """Return None if eligible, else the rejection reason."""
    model, provider = entry.model, entry.model.provider
    if not provider.is_enabled:
        return "provider disabled"
    if not model.is_enabled or not model.is_approved:
        return "model not enabled/approved"
    missing = set(requirements) - model.capabilities()
    if missing:
        return f"missing capabilities: {', '.join(sorted(missing))}"
    if task is not None and task.is_confidential and not (provider.is_local or provider.allow_confidential_data):
        return "privacy policy: confidential task cannot use this provider"
    if provider.circuit_open:
        return "circuit breaker open (provider unhealthy)"
    if provider.health_status == "auth_error":
        return "provider has invalid credentials"
    if model.daily_request_quota is not None:
        start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if UsageRecord.objects.filter(model=model, created_at__gte=start).count() >= model.daily_request_quota:
            return "configured daily quota reached"
    if model.is_paid:
        if not company.paid_models_enabled:
            return "paid models disabled by Founder"
        if not entry.allow_paid:
            return "paid model not explicitly allowed for this employee"
        ok, reason = budget.check_paid_call(est_cost, employee=employee, project=getattr(task, "project", None), provider=provider, task=task)
        if not ok:
            return f"budget: {reason}"
    return None


def route_chat(employee, messages, *, requirements=(), task=None, execution=None, json_mode=False,
               max_tokens=None, validator=None, sleep=time.sleep):
    company = CompanySettings.get()
    if company.emergency_stop:
        raise NoModelAvailable([{"model": "-", "outcome": "emergency stop active"}])
    attempts = []
    entries = list(employee.model_policies.select_related("model__provider").order_by("priority"))
    tried = set()
    first_eligible = True
    for entry in entries:
        model = entry.model
        if model.pk in tried:  # prevents fallback loops
            continue
        tried.add(model.pk)
        mt = max_tokens or model.max_output_tokens
        est_cost = model.estimate_cost(_approx_tokens(messages), mt)
        reason = eligibility(entry, requirements, company, employee=employee, task=task, est_cost=est_cost)
        if reason:
            attempts.append({"model": str(model), "outcome": f"rejected: {reason}"})
            continue
        adapter = get_adapter(model.provider)
        for attempt in range(MAX_RETRIES_PER_MODEL + 1):
            started = time.monotonic()
            usage = UsageRecord(
                employee=employee, task=task, execution=execution, department_id=employee.department_id,
                project=getattr(task, "project", None), provider=model.provider, model=model, was_fallback=not first_eligible,
            )
            try:
                result = adapter.chat(model.model_identifier, messages, json_mode=json_mode and model.supports_json, max_tokens=mt)
                usage.latency_ms = int((time.monotonic() - started) * 1000)
                usage.input_tokens, usage.output_tokens, usage.tokens_reported = result.input_tokens, result.output_tokens, result.tokens_reported
                usage.estimated_cost_usd = model.estimate_cost(result.input_tokens, result.output_tokens) if result.tokens_reported else est_cost if model.is_paid else 0
                if validator is not None:
                    validator(result.text)  # raises GatewayError(INVALID_OUTPUT)
                usage.save()
                _mark_success(model.provider)
                attempts.append({"model": str(model), "outcome": "selected", "attempt": attempt + 1})
                return RouteResult(result.text, model, attempts, usage)
            except GatewayError as err:
                usage.latency_ms = int((time.monotonic() - started) * 1000)
                usage.success, usage.error_kind = False, err.kind
                usage.save()
                if err.kind != GatewayError.INVALID_OUTPUT:
                    _mark_failure(model.provider, err)
                attempts.append({"model": str(model), "outcome": f"error: {err.kind}", "attempt": attempt + 1, "detail": err.message[:200]})
                if err.retryable and attempt < MAX_RETRIES_PER_MODEL and not model.provider.circuit_open:
                    delay = min(err.retry_after or BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
                    sleep(delay)
                    continue
                break  # non-retryable or retries exhausted -> fall back to next model
        first_eligible = False
    notify_leadership("models_exhausted", f"All permitted models failed for {employee.name}",
                      "; ".join(f"{a['model']}: {a['outcome']}" for a in attempts)[:1000])
    raise NoModelAvailable(attempts)
