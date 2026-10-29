from django.contrib import messages
from django.db.models import Avg, Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.core import audit
from apps.core.forms import AIModelForm, ProviderForm
from apps.core.permissions import can, require

from .adapters import GatewayError, get_adapter
from .models import AIModel, ModelProvider, UsageRecord


def provider_list(request):
    require(request.user, "view_models")
    providers = ModelProvider.objects.annotate(
        n_models=Count("models", distinct=True), calls=Count("usagerecord", distinct=True),
        failures=Count("usagerecord", filter=Q(usagerecord__success=False), distinct=True),
        latency=Avg("usagerecord__latency_ms"), cost=Sum("usagerecord__estimated_cost_usd"))
    models = AIModel.objects.select_related("provider").annotate(n_employees=Count("employee_policies"))
    form = None
    if can(request.user, "manage_models"):
        form = ProviderForm(request.POST or None, initial={"base_url": ModelProvider.DEFAULT_URLS[ModelProvider.OLLAMA],
                                                          "kind": ModelProvider.OLLAMA, "is_local": True})
        if request.method == "POST" and form.is_valid():
            p = form.save()
            audit.record("provider.created", target=p, request=request, details={"kind": p.kind, "paid": p.is_paid})
            messages.success(request, f"Provider {p.name} added. Run a health check, then add models.")
            return redirect("provider_detail", p.pk)
    return render(request, "models/list.html", {"providers": providers, "models": models, "form": form,
                                                "default_urls": ModelProvider.DEFAULT_URLS})


def provider_detail(request, pk):
    require(request.user, "view_models")
    provider = get_object_or_404(ModelProvider, pk=pk)
    manage = can(request.user, "manage_models")
    form = ProviderForm(request.POST or None, instance=provider) if manage else None
    mform = AIModelForm(request.POST or None, initial={"provider": provider}) if manage else None
    discovered = None
    if request.method == "POST":
        require(request.user, "manage_models")
        action = request.POST.get("action", "save")
        if action == "health":
            _health_check(provider)
            audit.record("provider.health_checked", target=provider, request=request, details={"status": provider.health_status})
            messages.info(request, f"Health: {provider.get_health_status_display()} {provider.last_error}")
            return redirect("provider_detail", pk)
        if action == "reset_circuit":
            provider.circuit_open_until, provider.consecutive_failures = None, 0
            provider.health_status = "unknown"
            provider.save()
            audit.record("provider.circuit_reset", target=provider, request=request)
            return redirect("provider_detail", pk)
        if action == "discover":
            try:
                discovered = get_adapter(provider).list_models()
            except (GatewayError, AttributeError) as exc:
                messages.error(request, f"Model discovery unavailable: {exc}")
        elif action == "add_model":
            if mform.is_valid():
                m = mform.save(commit=False)
                m.provider = provider
                m.save()
                audit.record("model.created", target=m, request=request, details={"model": m.model_identifier, "approved": m.is_approved})
                messages.success(request, f"Model {m.display_name} added.")
                return redirect("provider_detail", pk)
        elif action == "toggle_model":
            m = get_object_or_404(AIModel, pk=request.POST.get("model"), provider=provider)
            field = request.POST.get("field")
            if field in ("is_enabled", "is_approved"):
                setattr(m, field, not getattr(m, field))
                m.save(update_fields=[field])
                audit.record("model.updated", target=m, request=request, details={field: getattr(m, field)})
            return redirect("provider_detail", pk)
        elif form.is_valid():
            was_paid = ModelProvider.objects.get(pk=pk).is_paid
            form.save()
            audit.record("provider.updated", target=provider, request=request,
                         details={"fields": [f for f in form.changed_data if f != "api_key"], "api_key_changed": bool(form.cleaned_data.get("api_key"))})
            if provider.is_paid and not was_paid:
                audit.record("provider.marked_paid", target=provider, request=request)
            messages.success(request, "Provider saved.")
            return redirect("provider_detail", pk)
    usage = UsageRecord.objects.filter(provider=provider).select_related("model", "employee")[:30]
    return render(request, "models/provider.html", {"provider": provider, "form": form, "mform": mform, "manage": manage,
                                                    "models": provider.models.all(), "usage": usage, "discovered": discovered})


def _health_check(provider):
    provider.last_health_check = timezone.now()
    try:
        detail = get_adapter(provider).health_check()
        provider.health_status, provider.last_error, provider.consecutive_failures = "healthy", "", 0
        provider.circuit_open_until = None
        provider.save()
        return True, detail
    except GatewayError as exc:
        provider.health_status = "auth_error" if exc.kind == GatewayError.INVALID_CREDENTIALS else "down"
        provider.last_error = exc.message[:500]
        provider.save()
        return False, exc.message


def model_detail(request, pk):
    require(request.user, "view_models")
    model = get_object_or_404(AIModel.objects.select_related("provider"), pk=pk)
    form = AIModelForm(request.POST or None, instance=model) if can(request.user, "manage_models") else None
    if request.method == "POST":
        require(request.user, "manage_models")
        if form.is_valid():
            if "supports_json" in form.changed_data or "supports_tools" in form.changed_data:
                model.capabilities_verified_at = None  # capability flags changed: must be re-verified
            form.save()
            audit.record("model.updated", target=model, request=request, details={"fields": form.changed_data})
            messages.success(request, "Model saved.")
            return redirect("model_detail", pk)
    return render(request, "models/model.html", {"model": model, "form": form,
                                                 "employees": model.employee_policies.select_related("employee")})
