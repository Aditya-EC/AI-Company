"""Model routing, fallback, error classification, circuit breaker, paid-model and budget gates."""
from decimal import Decimal

import pytest

from apps.core.models import Budget, Incident
from apps.gateway import router
from apps.gateway.adapters import GatewayError
from apps.gateway.models import EmployeeModelPolicy, ModelProvider, UsageRecord
from apps.gateway.router import NoModelAvailable, route_chat

from .conftest import make_model, make_provider, make_task, no_sleep

MSG = [{"role": "user", "content": "hello"}]


@pytest.fixture
def two_models(db, llm, employee, company):
    """Primary = local model from conftest; secondary = a second local provider on the same fake server."""
    secondary = make_model(make_provider("Backup Ollama", llm.url), ident="backup-model")
    EmployeeModelPolicy.objects.create(employee=employee, model=secondary, priority=2)
    return employee, secondary


def test_primary_model_selected_and_usage_recorded(llm, employee, company):
    llm.script = [llm.ollama("hi there")]
    res = route_chat(employee, MSG, sleep=no_sleep)
    assert res.text == "hi there"
    assert res.model.model_identifier == "fake-model:latest"
    u = UsageRecord.objects.get()
    assert u.success and u.tokens_reported and (u.input_tokens, u.output_tokens) == (12, 30)
    assert not u.was_fallback
    assert llm.requests[0]["path"] == "/api/chat"


def test_rate_limit_retries_with_backoff_then_succeeds(llm, employee, company):
    sleeps = []
    llm.script = [(429, {"error": "rate limited"}), llm.ollama("ok")]
    res = route_chat(employee, MSG, sleep=sleeps.append)
    assert res.text == "ok"
    assert sleeps == [1.0]  # bounded exponential backoff, first step
    assert UsageRecord.objects.filter(success=False, error_kind="rate_limited").count() == 1


def test_outage_falls_back_to_secondary(llm, two_models):
    employee, secondary = two_models
    llm.script = [(503, "down"), (503, "down"), (503, "down"), llm.ollama("from backup")]
    res = route_chat(employee, MSG, sleep=no_sleep)
    assert res.model == secondary
    assert UsageRecord.objects.filter(success=True).get().was_fallback
    outcomes = [a["outcome"] for a in res.attempts]
    assert outcomes.count("error: transient") == 3 and outcomes[-1] == "selected"


def test_invalid_credentials_not_retried_and_flagged(llm, employee, company):
    cloud = make_provider("Cloud", llm.url + "/v1", kind="openai", credential_ref="env:VCOOS_TEST_MISSING_KEY")
    model = make_model(cloud, ident="gpt-test")
    EmployeeModelPolicy.objects.filter(employee=employee).delete()
    EmployeeModelPolicy.objects.create(employee=employee, model=model, priority=1)
    with pytest.raises(NoModelAvailable) as exc:
        route_chat(employee, MSG, sleep=no_sleep)
    assert any("invalid_credentials" in a["outcome"] for a in exc.value.attempts)
    cloud.refresh_from_db()
    assert cloud.health_status == "auth_error"
    assert Incident.objects.filter(category="provider").exists()
    assert len(llm.requests) == 0  # never sent a request without credentials


def test_quota_exhausted_distinguished_from_rate_limit(llm, two_models):
    employee, secondary = two_models
    llm.script = [(429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}), llm.ollama("ok")]
    res = route_chat(employee, MSG, sleep=no_sleep)
    assert res.attempts[0]["outcome"] == "error: quota_exhausted"  # no retry on the same model
    assert res.model == secondary


def test_unsupported_capability_skipped(llm, two_models):
    employee, secondary = two_models
    secondary.supports_tools = True
    secondary.save()
    llm.script = [llm.ollama("ok")]
    res = route_chat(employee, MSG, requirements=["tools"], sleep=no_sleep)
    assert res.model == secondary
    assert "missing capabilities: tools" in res.attempts[0]["outcome"]


def test_circuit_breaker_opens_and_skips_provider(llm, two_models):
    employee, secondary = two_models
    llm.default = (503, "down")
    with pytest.raises(NoModelAvailable):
        route_chat(employee, MSG, sleep=no_sleep)
    primary = employee.model_policies.get(priority=1).model.provider
    primary.refresh_from_db()
    assert primary.circuit_open and primary.health_status == "down"
    llm.default = None
    llm.script = [llm.ollama("ok")]
    secondary.provider.circuit_open_until = None
    secondary.provider.health_status = "unknown"
    secondary.provider.save()
    res = route_chat(employee, MSG, sleep=no_sleep)
    assert "circuit breaker open" in res.attempts[0]["outcome"] and res.model == secondary


def test_invalid_output_triggers_fallback_with_validation(llm, two_models):
    employee, secondary = two_models

    def validator(text):
        if "{" not in text:
            raise GatewayError(GatewayError.INVALID_OUTPUT, "not json")

    llm.script = [llm.ollama("plain text"), llm.ollama('{"ok": 1}')]
    res = route_chat(employee, MSG, validator=validator, sleep=no_sleep)
    assert res.model == secondary and res.text == '{"ok": 1}'


def test_paid_model_blocked_by_default(llm, employee, company):
    paid = make_provider("Paid", llm.url, is_paid=True)
    model = make_model(paid, ident="paid-model", price_input_per_mtok=Decimal("3"), price_output_per_mtok=Decimal("15"))
    EmployeeModelPolicy.objects.filter(employee=employee).delete()
    EmployeeModelPolicy.objects.create(employee=employee, model=model, priority=1, allow_paid=True)
    with pytest.raises(NoModelAvailable) as exc:
        route_chat(employee, MSG, sleep=no_sleep)
    assert "paid models disabled by Founder" in exc.value.attempts[0]["outcome"]
    assert llm.requests == []


def test_paid_model_requires_budget_and_explicit_permission(llm, employee, company):
    company.paid_models_enabled = True
    company.save()
    paid = make_provider("Paid", llm.url, is_paid=True)
    model = make_model(paid, ident="paid-model", price_input_per_mtok=Decimal("3"), price_output_per_mtok=Decimal("15"))
    EmployeeModelPolicy.objects.filter(employee=employee).delete()
    policy = EmployeeModelPolicy.objects.create(employee=employee, model=model, priority=1, allow_paid=False)
    with pytest.raises(NoModelAvailable) as exc:
        route_chat(employee, MSG, sleep=no_sleep)
    assert "not explicitly allowed" in exc.value.attempts[0]["outcome"]
    policy.allow_paid = True
    policy.save()
    with pytest.raises(NoModelAvailable) as exc:  # no company budget -> fail-safe deny
        route_chat(employee, MSG, sleep=no_sleep)
    assert "no company AI budget" in exc.value.attempts[0]["outcome"]
    Budget.objects.create(scope=Budget.COMPANY, period=Budget.MONTHLY, limit_usd=Decimal("1.00"))
    llm.script = [llm.ollama("paid ok", pin=1000, pout=1000)]
    res = route_chat(employee, MSG, sleep=no_sleep)
    assert res.text == "paid ok"
    assert UsageRecord.objects.get(success=True).estimated_cost_usd == Decimal("0.018")


def test_budget_exceeded_denies_paid_call(llm, employee, company):
    company.paid_models_enabled = True
    company.save()
    paid = make_provider("Paid", llm.url, is_paid=True)
    model = make_model(paid, ident="paid-model", price_input_per_mtok=Decimal("3"), price_output_per_mtok=Decimal("15"))
    EmployeeModelPolicy.objects.filter(employee=employee).delete()
    EmployeeModelPolicy.objects.create(employee=employee, model=model, priority=1, allow_paid=True)
    Budget.objects.create(scope=Budget.COMPANY, period=Budget.MONTHLY, limit_usd=Decimal("0.01"))
    UsageRecord.objects.create(employee=employee, provider=paid, model=model, estimated_cost_usd=Decimal("0.0099"))
    with pytest.raises(NoModelAvailable) as exc:
        route_chat(employee, MSG, sleep=no_sleep)
    assert "budget:" in exc.value.attempts[0]["outcome"]


def test_confidential_task_never_sent_to_cloud(llm, employee, company, dept):
    cloud = make_provider("Cloud", llm.url, kind="openai", credential_ref="env:X")
    model = make_model(cloud, ident="cloud")
    EmployeeModelPolicy.objects.filter(employee=employee).delete()
    EmployeeModelPolicy.objects.create(employee=employee, model=model, priority=1)
    task = make_task(dept, employee, is_confidential=True)
    with pytest.raises(NoModelAvailable) as exc:
        route_chat(employee, MSG, task=task, sleep=no_sleep)
    assert "privacy policy" in exc.value.attempts[0]["outcome"]


def test_unapproved_model_rejected_and_no_loop(llm, employee, company):
    m = employee.model_policies.get().model
    m.is_approved = False
    m.save()
    with pytest.raises(NoModelAvailable) as exc:
        route_chat(employee, MSG, sleep=no_sleep)
    assert len(exc.value.attempts) == 1 and llm.requests == []


def test_openai_compatible_adapter_with_encrypted_key(llm, employee, company):
    cloud = make_provider("OpenAI-compat", llm.url, kind="openai_compatible")
    cloud.set_api_key("good-key")
    cloud.save()
    assert "good-key" not in cloud.encrypted_api_key
    model = make_model(cloud, ident="compat")
    EmployeeModelPolicy.objects.filter(employee=employee).delete()
    EmployeeModelPolicy.objects.create(employee=employee, model=model, priority=1)
    llm.script = [llm.openai("compat ok")]
    assert route_chat(employee, MSG, sleep=no_sleep).text == "compat ok"
    assert llm.requests[0]["path"] == "/chat/completions" and llm.requests[0]["auth"] == "Bearer good-key"
