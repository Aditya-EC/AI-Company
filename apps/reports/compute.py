"""Report calculations. Every figure is computed from recorded data.

Performance is reported on several dimensions (output, accepted quality, rework, reliability,
timeliness and effort) - deliberately not as one quantity-based score. Costs are split into
estimates and verified billed charges.
"""
from collections import Counter
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from apps.core import budget
from apps.core.models import Budget, EmployeeProfile, Incident
from apps.gateway.models import AIModel, ModelProvider, UsageRecord
from apps.work.models import ApprovalRequest, Decision, Project, Release, Review, Task, TaskExecution, ToolCall

T = Task
ZERO = Decimal("0")


class Filters:
    def __init__(self, start=None, end=None, department=None, project=None, employee=None):
        today = timezone.localdate()
        self.start = start or today - timedelta(days=30)
        self.end = end or today
        self.department, self.project, self.employee = department, project, employee
        tz = timezone.get_current_timezone()
        self.start_dt = timezone.make_aware(datetime.combine(self.start, time.min), tz)
        self.end_dt = timezone.make_aware(datetime.combine(self.end, time.max), tz)

    def describe(self):
        parts = [f"{self.start} to {self.end}"]
        if self.department:
            parts.append(f"department: {self.department.name}")
        if self.project:
            parts.append(f"project: {self.project.name}")
        if self.employee:
            parts.append(f"employee: {self.employee.name}")
        return ", ".join(parts)

    def tasks(self, qs=None):
        qs = (qs if qs is not None else Task.objects.all()).exclude(output_kind__in=[T.PLAN])
        if self.department:
            qs = qs.filter(department=self.department)
        if self.project:
            qs = qs.filter(project=self.project)
        if self.employee:
            qs = qs.filter(assigned_employee=self.employee)
        return qs

    def usage(self):
        qs = UsageRecord.objects.filter(created_at__range=(self.start_dt, self.end_dt))
        if self.department:
            qs = qs.filter(department=self.department)
        if self.project:
            qs = qs.filter(project=self.project)
        if self.employee:
            qs = qs.filter(employee=self.employee)
        return qs


def _pct(num, den):
    return f"{round(100 * num / den)}%" if den else "—"


def _money(v):
    return f"${(v or ZERO):.4f}"


def _section_metrics(title, metrics, note=""):
    return {"title": title, "kind": "metrics", "metrics": metrics, "note": note}


def _section_table(title, columns, rows, note=""):
    return {"title": title, "kind": "table", "columns": columns, "rows": rows, "note": note}


def _report(title, f, sections, notes=()):
    return {"title": title, "filters": f.describe(), "generated_at": timezone.now().strftime("%Y-%m-%d %H:%M %Z"),
            "sections": sections, "notes": list(notes)}


# ------------------------------------------------------------------ employee metrics

def employee_metrics(emp, f):
    tasks = f.tasks(Task.objects.filter(assigned_employee=emp)).filter(updated_at__range=(f.start_dt, f.end_dt))
    completed = tasks.filter(status=T.COMPLETED)
    failed = tasks.filter(status=T.FAILED).count()
    reviews = Review.objects.filter(task__assigned_employee=emp, created_at__range=(f.start_dt, f.end_dt))
    accepted, rejected = reviews.filter(outcome="accept").count(), reviews.filter(outcome="reject").count()
    execs = TaskExecution.objects.filter(employee=emp, status=TaskExecution.SUCCEEDED, finished_at__isnull=False,
                                         started_at__range=(f.start_dt, f.end_dt))
    durations = [(e.finished_at - e.started_at).total_seconds() for e in execs]
    steps = [len(e.steps) for e in execs]
    with_deadline = completed.filter(deadline__isnull=False)
    on_time = sum(1 for t in with_deadline if t.completed_at and t.completed_at <= t.deadline)
    usage = f.usage().filter(employee=emp)
    tool_counts = Counter(ToolCall.objects.filter(execution__employee=emp, created_at__range=(f.start_dt, f.end_dt))
                          .values_list("tool_slug", flat=True))
    model_counts = Counter(usage.filter(success=True).values_list("model__display_name", flat=True))
    tokens = usage.filter(tokens_reported=True).aggregate(i=Sum("input_tokens"), o=Sum("output_tokens"))
    return {
        "employee": emp, "completed": completed.count(), "failed": failed, "accepted": accepted, "rejected": rejected,
        "rework_rate": _pct(rejected, accepted + rejected), "failure_rate": _pct(failed, completed.count() + failed),
        "acceptance_rate": _pct(accepted, accepted + rejected),
        "avg_exec_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "avg_steps": round(sum(steps) / len(steps), 1) if steps else None,
        "deadline": f"{on_time}/{with_deadline.count()} on time" if with_deadline.exists() else "no deadlines",
        "tools": dict(tool_counts.most_common(8)), "models": dict(model_counts.most_common(5)),
        "calls": usage.count(), "cost": usage.aggregate(c=Sum("estimated_cost_usd"))["c"] or ZERO,
        "tokens": (tokens["i"] or 0) + (tokens["o"] or 0),
    }


EMP_COLUMNS = ["Employee", "Completed", "Accepted / rejected", "Rework rate", "Failure rate", "Avg exec (s)", "Avg steps",
               "Deadlines", "Model calls", "Est. cost"]


def _emp_row(m):
    return [f"{m['employee'].name} ({m['employee'].employee_id})", m["completed"], f"{m['accepted']} / {m['rejected']}",
            m["rework_rate"], m["failure_rate"], m["avg_exec_seconds"] if m["avg_exec_seconds"] is not None else "—",
            m["avg_steps"] if m["avg_steps"] is not None else "—", m["deadline"], m["calls"], _money(m["cost"])]


PERFORMANCE_NOTE = ("Performance is shown across output, review acceptance, rework, failures, timeliness and effort "
                    "(average steps per execution as a difficulty proxy). No single score is computed from task counts.")


# ------------------------------------------------------------------ reports

def company_report(f):
    tasks = f.tasks()
    created = tasks.filter(created_at__range=(f.start_dt, f.end_dt)).count()
    done = tasks.filter(status=T.COMPLETED, completed_at__range=(f.start_dt, f.end_dt)).count()
    failed = tasks.filter(status=T.FAILED, completed_at__range=(f.start_dt, f.end_dt)).count()
    projects = Project.objects.all()
    if f.department:
        projects = projects.filter(department=f.department)
    if f.project:
        projects = projects.filter(pk=f.project.pk)
    proj_rows = []
    for p in projects.annotate(total=Count("tasks", filter=~Q(tasks__status=T.CANCELLED) & ~Q(tasks__output_kind=T.PLAN)),
                               done=Count("tasks", filter=Q(tasks__status=T.COMPLETED) & ~Q(tasks__output_kind=T.PLAN))):
        proj_rows.append([f"{p.code} {p.name}", p.get_status_display(), p.get_priority_display(), f"{p.done}/{p.total}",
                          p.created_at.strftime("%Y-%m-%d")])
    delivered = Decision.objects.filter(kind="project_status", outcome=Project.COMPLETED, created_at__range=(f.start_dt, f.end_dt))
    releases = Release.objects.filter(status=Release.APPROVED, created_at__range=(f.start_dt, f.end_dt)).select_related("project")
    decisions = Decision.objects.filter(created_at__range=(f.start_dt, f.end_dt)).select_related("made_by", "project", "proposal")
    if f.project:
        decisions = decisions.filter(project=f.project)
    budgets = [(b, budget.spent(b)) for b in Budget.objects.filter(is_active=True)]
    approvals = ApprovalRequest.objects.filter(status=ApprovalRequest.PENDING)
    incidents = Incident.objects.filter(created_at__range=(f.start_dt, f.end_dt))
    return _report("Company report", f, [
        _section_metrics("Overall activity", [("Tasks created", created), ("Tasks completed", done), ("Tasks failed", failed),
                                              ("Completion rate", _pct(done, done + failed)),
                                              ("Active projects", projects.filter(status=Project.ACTIVE).count()),
                                              ("AI employees", EmployeeProfile.objects.count()),
                                              ("Pending approvals", approvals.count()), ("Incidents", incidents.count())]),
        _section_table("Projects", ["Project", "Status", "Priority", "Tasks done", "Created"], proj_rows),
        _section_table("Delivery history", ["When", "Event", "Project"],
                       [[d.created_at.strftime("%Y-%m-%d"), "Project completed", d.project.name if d.project else ""] for d in delivered] +
                       [[r.created_at.strftime("%Y-%m-%d"), f"Release v{r.version} approved", r.project.name] for r in releases]),
        _section_table("Major decisions", ["When", "Kind", "Outcome", "By", "Subject", "Rationale"],
                       [[d.created_at.strftime("%Y-%m-%d %H:%M"), d.get_kind_display(), d.outcome,
                         f"{d.made_by.username if d.made_by else '?'} ({d.authority})",
                         (d.project.name if d.project else d.proposal.title if d.proposal else ""), d.rationale[:200]] for d in decisions[:100]]),
        _section_table("Budget utilization", ["Budget", "Spent", "Limit", "Used"],
                       [[str(b), _money(s), _money(b.limit_usd), _pct(s, b.limit_usd)] for b, s in budgets],
                       note="Spend uses verified billed cost where recorded, otherwise estimates."),
        _section_table("Outstanding approvals", ["Requested", "Kind", "Title", "Authority"],
                       [[a.created_at.strftime("%Y-%m-%d"), a.get_kind_display(), a.title, a.get_required_authority_display()] for a in approvals]),
        _section_table("Operational incidents", ["When", "Severity", "Category", "Title", "Resolved"],
                       [[i.created_at.strftime("%Y-%m-%d %H:%M"), i.get_severity_display(), i.category, i.title, "yes" if i.is_resolved else "no"]
                        for i in incidents]),
    ])


def department_report(dept, f):
    f.department = dept
    tasks = f.tasks()
    now = timezone.now()
    in_range = tasks.filter(updated_at__range=(f.start_dt, f.end_dt))
    workload = dict(tasks.exclude(status__in=T.TERMINAL).values_list("status").annotate(n=Count("id")))
    overdue = tasks.exclude(status__in=T.TERMINAL).filter(deadline__lt=now)
    late = [t for t in in_range.filter(status=T.COMPLETED, deadline__isnull=False) if t.completed_at and t.completed_at > t.deadline]
    blockers = tasks.filter(status__in=[T.BLOCKED, T.WAITING, T.PAUSED, T.AWAITING_APPROVAL])
    reviews = Review.objects.filter(task__department=dept, created_at__range=(f.start_dt, f.end_dt))
    acc, rej = reviews.filter(outcome="accept").count(), reviews.filter(outcome="reject").count()
    emps = [employee_metrics(e, f) for e in EmployeeProfile.objects.filter(department=dept)]
    return _report(f"Department report: {dept.name}", f, [
        _section_metrics("Workload & delivery", [*[(dict(T.STATUSES).get(s, s), n) for s, n in sorted(workload.items())],
                                                 ("Completed in period", in_range.filter(status=T.COMPLETED).count()),
                                                 ("Failed in period", in_range.filter(status=T.FAILED).count()),
                                                 ("Overdue open tasks", overdue.count()), ("Completed late", len(late))]),
        _section_metrics("Quality", [("Reviews accepted", acc), ("Reviews rejected", rej), ("Acceptance rate", _pct(acc, acc + rej))]),
        _section_table("Delays", ["Task", "Deadline", "Status"],
                       [[f"{t.code} {t.title}", t.deadline.strftime("%Y-%m-%d %H:%M"), t.get_status_display()] for t in overdue] +
                       [[f"{t.code} {t.title}", t.deadline.strftime("%Y-%m-%d %H:%M"), "completed late"] for t in late]),
        _section_table("Failed tasks", ["Task", "Error"], [[f"{t.code} {t.title}", t.error[:200]] for t in in_range.filter(status=T.FAILED)]),
        _section_table("Current blockers", ["Task", "Status", "Reason"],
                       [[f"{t.code} {t.title}", t.get_status_display(), t.status_reason[:200]] for t in blockers]),
        _section_table("Employees", EMP_COLUMNS, [_emp_row(m) for m in emps], note=PERFORMANCE_NOTE),
    ])


def employee_report(emp, f):
    m = employee_metrics(emp, f)
    tasks = f.tasks(Task.objects.filter(assigned_employee=emp)).filter(updated_at__range=(f.start_dt, f.end_dt))
    return _report(f"Employee report: {emp.name}", f, [
        _section_metrics("Performance", [("Tasks completed", m["completed"]), ("Accepted after review", m["accepted"]),
                                         ("Rejected (rework)", m["rejected"]), ("Rework rate", m["rework_rate"]),
                                         ("Failure rate", m["failure_rate"]),
                                         ("Avg execution time", f"{m['avg_exec_seconds']} s" if m["avg_exec_seconds"] is not None else "—"),
                                         ("Avg steps per execution (effort)", m["avg_steps"] if m["avg_steps"] is not None else "—"),
                                         ("Deadline performance", m["deadline"]), ("Model calls", m["calls"]),
                                         ("Reported tokens", m["tokens"]), ("Estimated cost", _money(m["cost"]))], note=PERFORMANCE_NOTE),
        _section_table("Tool usage", ["Tool", "Calls"], [[k, v] for k, v in m["tools"].items()]),
        _section_table("Model usage", ["Model", "Successful calls"], [[k or "?", v] for k, v in m["models"].items()]),
        _section_table("Tasks in period", ["Task", "Status", "Updated", "Review"],
                       [[f"{t.code} {t.title}", t.get_status_display(), t.updated_at.strftime("%Y-%m-%d"),
                         ", ".join(t.reviews.values_list("outcome", flat=True)) or "—"] for t in tasks[:200]]),
    ])


def financial_report(f):
    usage = f.usage()
    rows = []
    for p in ModelProvider.objects.all():
        u = usage.filter(provider=p)
        agg = u.aggregate(est=Sum("estimated_cost_usd"), billed=Sum("billed_cost_usd"), lat=Avg("latency_ms"))
        tok = u.filter(tokens_reported=True).aggregate(i=Sum("input_tokens"), o=Sum("output_tokens"))
        rows.append([p.name, "local" if p.is_local else "cloud", u.count(), u.filter(success=False).count(),
                     u.filter(was_fallback=True, success=True).count(), f"{tok['i'] or 0} / {tok['o'] or 0}", _money(agg["est"]),
                     _money(agg["billed"]) if u.filter(billed_cost_usd__isnull=False).exists() else "not available",
                     f"{round(agg['lat'])} ms" if agg["lat"] else "—"])
    model_rows = []
    for m in AIModel.objects.select_related("provider"):
        u = usage.filter(model=m)
        if not u.exists():
            continue
        model_rows.append([f"{m.display_name} ({m.provider.name})", u.count(), u.filter(success=True).count(),
                           _money(u.aggregate(c=Sum("estimated_cost_usd"))["c"])])
    free_rows = []
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for m in AIModel.objects.filter(provider__is_local=False, price_input_per_mtok=0, price_output_per_mtok=0, provider__is_paid=False):
        n_today = UsageRecord.objects.filter(model=m, created_at__gte=today_start).count()
        free_rows.append([m.display_name, usage.filter(model=m).count(), n_today,
                          m.daily_request_quota if m.daily_request_quota is not None else "unknown"])
    errors = Counter(usage.filter(success=False).values_list("error_kind", flat=True))
    budgets = [(b, budget.spent(b)) for b in Budget.objects.filter(is_active=True)]
    est = usage.aggregate(c=Sum("estimated_cost_usd"))["c"] or ZERO
    billed_qs = usage.filter(billed_cost_usd__isnull=False)
    return _report("Financial & AI usage report", f, [
        _section_metrics("Totals", [("Model calls", usage.count()), ("Failed calls", usage.filter(success=False).count()),
                                    ("Successful fallback calls", usage.filter(was_fallback=True, success=True).count()),
                                    ("Estimated cost", _money(est)),
                                    ("Verified billed cost", _money(billed_qs.aggregate(c=Sum("billed_cost_usd"))["c"]) if billed_qs.exists() else "not available"),
                                    ("Calls with provider-reported tokens", usage.filter(tokens_reported=True).count())]),
        _section_table("Provider usage", ["Provider", "Type", "Calls", "Failures", "Fallback successes", "Tokens in / out",
                                          "Estimated cost", "Billed cost", "Avg latency"], rows,
                       note="Token counts include only calls where the provider reported them."),
        _section_table("Model usage", ["Model", "Calls", "Successful", "Estimated cost"], model_rows),
        _section_table("Free-tier usage (observable)", ["Model", "Calls in period", "Calls today", "Configured daily quota"], free_rows,
                       note="Free-tier limits change; quotas shown are the values configured by the Founder."),
        _section_table("Budgets", ["Budget", "Spent", "Limit", "Remaining"],
                       [[str(b), _money(s), _money(b.limit_usd), _money(max(b.limit_usd - s, ZERO))] for b, s in budgets]),
        _section_table("Failure statistics", ["Error kind", "Count"], [[k or "unknown", v] for k, v in errors.most_common()]),
    ], notes=["Estimated costs are computed from configured prices and token counts; they are not provider invoices.",
              "Verified billed cost appears only where provider billing data has been recorded."])
