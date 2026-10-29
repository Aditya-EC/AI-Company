"""Research proposals: agent output validation, CEO evaluation within authority, Founder override."""
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.core import audit
from apps.core.models import CompanySettings, Department
from apps.core.notify import notify_leadership
from apps.core.permissions import can

from . import services
from .models import Decision, Project, ProjectProposal, ProjectTemplate, Task, ToolCall

P = ProjectProposal
REQUIRED_TEXT = ["title", "problem_statement", "target_users", "technical_feasibility", "validation_plan"]
REQUIRED_LISTS = ["proposed_features", "risks", "assumptions", "uncertainties"]
OPTIONAL_TEXT = ["market_research", "revenue_hypothesis", "software_category"]
OPTIONAL_LISTS = ["recommended_stack"]


class ProposalError(Exception):
    pass


def _str_list(value, field, max_items=30):
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"'{field}' must be a list of non-empty strings")
    return [v.strip()[:1000] for v in value[:max_items]]


def validate_payload(data):
    """Validate a research agent's structured proposal. Raises ValueError with a fixable message."""
    if not isinstance(data, dict):
        raise ValueError("'proposal' must be a JSON object")
    clean = {}
    for f in REQUIRED_TEXT:
        if not isinstance(data.get(f), str) or not data[f].strip():
            raise ValueError(f"missing or empty text field '{f}'")
        clean[f] = data[f].strip()[:200 if f == "title" else 8000]
    for f in OPTIONAL_TEXT:
        if data.get(f) is not None:
            if not isinstance(data[f], str):
                raise ValueError(f"'{f}' must be text")
            clean[f] = data[f].strip()[:8000]
    for f in REQUIRED_LISTS:
        if f not in data:
            raise ValueError(f"missing list field '{f}' (use [] only if there are genuinely none)")
        clean[f] = _str_list(data[f], f)
    if not clean["proposed_features"]:
        raise ValueError("'proposed_features' must not be empty")
    for f in OPTIONAL_LISTS:
        if data.get(f) is not None:
            clean[f] = _str_list(data[f], f)
    evidence = data.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("'evidence' must be a list of {claim, source_url}")
    clean["evidence"] = []
    for item in evidence[:30]:
        if not isinstance(item, dict) or not isinstance(item.get("claim"), str) or not item["claim"].strip():
            raise ValueError("each evidence item needs a non-empty 'claim'")
        url = item.get("source_url") or ""
        if not isinstance(url, str) or (url and not url.startswith(("http://", "https://"))):
            raise ValueError("evidence 'source_url' must be an http(s) URL or empty")
        clean["evidence"].append({"claim": item["claim"].strip()[:1000], "source_url": url[:500]})
    for f in ("estimated_effort_days", "estimated_cost_usd"):
        v = data.get(f)
        if v is None:
            if f == "estimated_effort_days":
                raise ValueError("'estimated_effort_days' is required (a number)")
            continue
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            raise ValueError(f"'{f}' must be a number")
        if d < 0:
            raise ValueError(f"'{f}' must not be negative")
        clean[f] = d
    conf = data.get("confidence")
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 100:
        raise ValueError("'confidence' must be a number from 0 to 100")
    clean["confidence"] = int(conf)
    return clean


def fetched_urls(task):
    """URLs this task actually retrieved successfully with web.fetch (system-observed, not agent-claimed)."""
    urls = set()
    for call in ToolCall.objects.filter(execution__task=task, tool_slug="web.fetch", allowed=True, success=True):
        if isinstance(call.input_data.get("url"), str):
            urls.add(call.input_data["url"])
    return urls


def create_from_agent(task, clean):
    retrieved = fetched_urls(task)
    for item in clean["evidence"]:
        item["retrieved"] = bool(item["source_url"]) and item["source_url"] in retrieved
    previous = None
    if task.parent_task_id:
        previous = task.parent_task.produced_proposals.order_by("-id").first()
    proposal = P.objects.create(**clean, department=task.department, proposed_by=task.assigned_employee, source_task=task,
                                revision_of=previous, status=P.SUBMITTED)
    audit.record("proposal.submitted", employee=task.assigned_employee, target=proposal,
                 details={"task": task.pk, "evidence": len(clean["evidence"]), "retrieved": sum(e["retrieved"] for e in clean["evidence"])})
    notify_leadership("proposal", f"New proposal {proposal.code}: {proposal.title}", proposal.problem_statement[:300],
                      f"/proposals/{proposal.pk}/")
    return proposal


def commission_research(*, brief, department, user, employee=None, category=""):
    company = CompanySettings.get()
    task = Task.objects.create(
        title=f"Research: {brief[:180]}", output_kind=Task.PROPOSAL, department=department, assigned_employee=employee,
        description=(f"Investigate this opportunity and prepare a project proposal.\n\nBRIEF:\n{brief}\n\n"
                     f"Software category: {category or 'choose the most suitable permitted category'}\n"
                     f"COMPANY STRATEGY:\n{company.strategy or '(not specified)'}"),
        acceptance_criteria="Complete structured proposal; verified facts are cited with retrieved sources; assumptions and "
                            "uncertainties stated explicitly.",
        requires_review=False, required_skills=["research"], created_by=user,
        max_steps=max(company.default_max_steps, 12), max_retries=company.default_max_retries,
        max_runtime_seconds=company.default_max_runtime_seconds)
    audit.record("proposal.research_commissioned", user=user, target=task, details={"brief": brief[:300]})
    services.queue_task(task, user=user, reason="research commissioned")
    return task


DECISIONS = {"approve", "reject", "defer", "revision", "escalate"}
STATUS_FOR = {"approve": P.APPROVED, "reject": P.REJECTED, "defer": P.DEFERRED, "revision": P.REVISION, "escalate": P.ESCALATED}


def _check_authority(proposal, user, outcome):
    """Return ('founder'|'ceo', is_override). Raises ProposalError when outside the user's authority."""
    if user.is_founder:
        is_override = proposal.status in (P.APPROVED, P.REJECTED, P.DEFERRED, P.REVISION) and proposal.decisions.exists()
        return "founder", is_override
    if not can(user, "decide_proposals"):
        raise ProposalError("You are not authorized to decide proposals")
    if proposal.status not in (P.SUBMITTED, P.DEFERRED):
        raise ProposalError("Only the Founder can change a decided or escalated proposal")
    if outcome == "approve":
        limit = CompanySettings.get().ceo_approval_limit_usd
        if proposal.estimated_cost_usd is None:
            raise ProposalError("No cost estimate: approval needs the Founder. Escalate or request a revision.")
        if proposal.estimated_cost_usd > limit:
            raise ProposalError(f"Estimated cost ${proposal.estimated_cost_usd} exceeds the CEO approval limit ${limit}; escalate to the Founder.")
    return "ceo", False


def decide(proposal, *, user, outcome, rationale="", planning_department=None, planner=None):
    if outcome not in DECISIONS:
        raise ProposalError("Unknown decision")
    if outcome in ("reject", "revision", "escalate") and not rationale.strip():
        raise ProposalError("A rationale is required for this decision")
    if outcome == "escalate" and user.is_founder:
        raise ProposalError("The Founder decides directly")
    authority, is_override = _check_authority(proposal, user, outcome)
    company = CompanySettings.get()
    if outcome == "approve" and company.permitted_software_categories and proposal.software_category and \
            proposal.software_category not in company.permitted_software_categories:
        raise ProposalError(f"Category '{proposal.software_category}' is not permitted by the Founder's software policy")
    previous = proposal.decisions.order_by("-created_at", "-id").first()
    effects = {}
    with transaction.atomic():
        old_status = proposal.status
        proposal.status = STATUS_FOR[outcome]
        proposal.save(update_fields=["status", "updated_at"])
        if is_override and old_status == P.APPROVED and outcome != "approve":
            effects["project_cancelled"] = _cancel_project_for(proposal, user)
        if outcome == "approve" and not Project.objects.filter(proposal=proposal).exists():
            project, plan_task = start_project(proposal, user=user, department=planning_department, planner=planner)
            effects.update(project=project.pk, planning_task=plan_task.pk)
        if outcome == "revision" and proposal.source_task_id:
            effects["revision_task"] = _revision_research(proposal, user, rationale).pk
        decision = Decision.objects.create(kind="override" if is_override else "proposal", outcome=outcome, rationale=rationale,
                                           effects=effects, proposal=proposal, made_by=user, authority=authority,
                                           overrides=previous if is_override else None,
                                           project=Project.objects.filter(proposal=proposal).first())
        audit.record("proposal.decided", user=user, target=proposal,
                     details={"outcome": outcome, "authority": authority, "override": is_override, "effects": effects})
    if authority == "ceo" and outcome != "escalate":
        from apps.core.notify import notify_founder

        notify_founder("ceo_decision", f"CEO decision on {proposal.code}: {outcome}", f"{proposal.title}. {rationale}"[:300],
                       f"/proposals/{proposal.pk}/")
    if outcome == "escalate":
        notify_leadership("proposal_escalated", f"{proposal.code} escalated to the Founder", rationale[:300], f"/proposals/{proposal.pk}/")
    return decision


def _cancel_project_for(proposal, user):
    project = Project.objects.filter(proposal=proposal).first()
    if project is None:
        return None
    from .projects import set_project_status

    set_project_status(project, Project.CANCELLED, user=user, reason=f"proposal {proposal.code} decision overridden")
    return project.pk


def _revision_research(proposal, user, feedback):
    src = proposal.source_task
    task = Task.objects.create(
        title=f"Revise proposal {proposal.code}: {proposal.title}"[:200], output_kind=Task.PROPOSAL, department=src.department,
        assigned_employee=src.assigned_employee, parent_task=src, created_by=user, requires_review=False,
        required_skills=src.required_skills, required_capabilities=src.required_capabilities, max_steps=src.max_steps, max_retries=src.max_retries,
        max_runtime_seconds=src.max_runtime_seconds,
        description=f"{src.description}\n\nDECISION FEEDBACK (must be addressed):\n{feedback}\n\nPREVIOUS PROPOSAL SUMMARY:\n"
                    f"{proposal.title}: {proposal.problem_statement[:2000]}",
        acceptance_criteria=src.acceptance_criteria)
    services.queue_task(task, user=user, reason="proposal revision requested")
    return task


def start_project(proposal, *, user, department=None, planner=None):
    """Create the project (and repository when the template uses one) plus a planning task."""
    from . import repo

    template = ProjectTemplate.objects.filter(category=proposal.software_category).first() if proposal.software_category else None
    department = department or proposal.department or Department.objects.filter(is_active=True).first()
    project = Project.objects.create(
        name=proposal.title, description=proposal.problem_statement, proposal=proposal, template=template,
        category=proposal.software_category, department=department, status=Project.PLANNING,
        plan_status=Project.PLAN_IN_PROGRESS, created_by=user,
        budget_usd=proposal.estimated_cost_usd)
    audit.record("project.created", user=user, target=project, details={"proposal": proposal.pk, "category": project.category})
    if (template is None or template.uses_repository) and repo.git_available():
        repo.init_repo(project, readme=f"# {project.name}\n\n{proposal.problem_statement}\n")
        project.has_repository = True
        project.save(update_fields=["has_repository"])
    from .projects import planning_brief

    plan_task = Task.objects.create(
        title=f"Plan: {project.name}"[:200], output_kind=Task.PLAN, project=project, department=department,
        assigned_employee=planner, created_by=user, requires_review=False, required_skills=["planning"],
        description=planning_brief(project), max_steps=10,
        acceptance_criteria="Every task has a clear description, testable acceptance criteria, a department and correct dependencies.")
    services.queue_task(plan_task, user=user, reason="project approved; planning started")
    return project, plan_task
