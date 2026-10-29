"""Budget enforcement. Fails safe: paid usage without a configured, sufficient budget is denied."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Budget


def _period_start(period):
    now = timezone.now()
    if period == Budget.DAILY:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == Budget.MONTHLY:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(days=365 * 100)


def spent(budget):
    from apps.gateway.models import UsageRecord

    qs = UsageRecord.objects.filter(created_at__gte=_period_start(budget.period))
    if budget.scope == Budget.DEPARTMENT:
        qs = qs.filter(department_id=budget.department_id)
    elif budget.scope == Budget.EMPLOYEE:
        qs = qs.filter(employee_id=budget.employee_id)
    elif budget.scope == Budget.PROJECT:
        qs = qs.filter(project_id=budget.project_id)
    elif budget.scope == Budget.PROVIDER:
        qs = qs.filter(provider_id=budget.provider_id)
    # Prefer verified billed cost when known, otherwise the estimate.
    total = Decimal("0")
    for billed, est in qs.values_list("billed_cost_usd", "estimated_cost_usd"):
        total += billed if billed is not None else est
    return total


def applicable_budgets(employee=None, project=None, provider=None):
    budgets = Budget.objects.filter(is_active=True)
    out = [b for b in budgets if b.scope == Budget.COMPANY]
    if employee is not None:
        out += [b for b in budgets if b.scope == Budget.DEPARTMENT and b.department_id == employee.department_id]
        out += [b for b in budgets if b.scope == Budget.EMPLOYEE and b.employee_id == employee.pk]
    if project is not None:
        out += [b for b in budgets if b.scope == Budget.PROJECT and b.project_id == project.pk]
    if provider is not None:
        out += [b for b in budgets if b.scope == Budget.PROVIDER and b.provider_id == provider.pk]
    return out


def check_paid_call(estimated_cost, *, employee=None, project=None, provider=None, task=None):
    """Return (allowed, reason). Only called for potentially chargeable calls."""
    estimated_cost = Decimal(estimated_cost)
    budgets = applicable_budgets(employee, project, provider)
    if not any(b.scope == Budget.COMPANY for b in budgets):
        return False, "no company AI budget configured (fail-safe deny)"
    for b in budgets:
        remaining = b.limit_usd - spent(b)
        if estimated_cost > remaining:
            return False, f"{b} exceeded: remaining ${remaining:.4f}, call estimate ${estimated_cost:.4f}"
    if employee is not None and employee.budget_limit_usd is not None:
        from apps.gateway.models import UsageRecord

        used = UsageRecord.objects.filter(employee=employee).aggregate(t=Coalesce(Sum("estimated_cost_usd"), Decimal("0")))["t"]
        if used + estimated_cost > employee.budget_limit_usd:
            return False, f"employee budget limit ${employee.budget_limit_usd} reached"
    if task is not None and task.max_cost_usd is not None:
        if task.actual_cost_usd + estimated_cost > task.max_cost_usd:
            return False, f"task cost limit ${task.max_cost_usd} reached"
    return True, "within budget"
