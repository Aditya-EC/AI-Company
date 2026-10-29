"""Proposal board, proposal evaluation, projects, plans, priorities and the Software Factory."""
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core import audit
from apps.core.permissions import can, require, scope_queryset

from . import projects as svc
from . import proposals as psvc
from . import repo
from .forms import CommissionResearchForm, ProjectPriorityForm, ProjectTemplateForm, ProposalDecisionForm, ProposalForm, ReleaseForm
from .models import Project, ProjectProposal, ProjectTemplate, Release, Task

P = ProjectProposal
COLUMNS = [("Submitted", [P.SUBMITTED, P.ESCALATED]), ("Revision / deferred", [P.REVISION, P.DEFERRED, P.DRAFT]),
           ("Approved", [P.APPROVED]), ("Rejected", [P.REJECTED])]


def proposal_board(request):
    require(request.user, "view_projects")
    qs = scope_queryset(request.user, "view_projects", P.objects.select_related("proposed_by", "department"), project_field=None)
    manage = can(request.user, "manage_projects")
    research_form = CommissionResearchForm(request.POST if request.POST.get("action") == "commission" else None) if manage else None
    if request.method == "POST":
        require(request.user, "manage_projects")
        if research_form is not None and research_form.is_valid():
            d = research_form.cleaned_data
            task = psvc.commission_research(brief=d["brief"], department=d["department"], employee=d["employee"],
                                            user=request.user, category=d["category"])
            messages.success(request, f"Research commissioned as {task.code}. The proposal appears here when the researcher submits it.")
            return redirect("task_detail", task.pk)
    research = scope_queryset(request.user, "view_tasks", Task.objects.filter(output_kind=Task.PROPOSAL).exclude(status__in=Task.TERMINAL))
    return render(request, "proposals/board.html", {
        "columns": [(name, list(qs.filter(status__in=sts)[:40])) for name, sts in COLUMNS],
        "research_form": research_form, "research_tasks": research.select_related("assigned_employee")[:20], "manage": manage,
    })


def proposal_create(request):
    require(request.user, "manage_projects")
    form = ProposalForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        p = form.save(commit=False)
        p.submitted_by_user, p.status = request.user, P.SUBMITTED
        p.evidence = []
        p.save()
        audit.record("proposal.submitted", user=request.user, target=p, details={"manual": True})
        messages.success(request, f"{p.code} submitted for evaluation.")
        return redirect("proposal_detail", p.pk)
    return render(request, "proposals/form.html", {"form": form})


def proposal_detail(request, pk):
    proposal = get_object_or_404(P.objects.select_related("proposed_by", "department", "source_task", "revision_of"), pk=pk)
    require(request.user, "view_projects", proposal)
    can_decide = request.user.is_founder or can(request.user, "decide_proposals")
    form = ProposalDecisionForm(request.POST or None) if can_decide else None
    if request.method == "POST":
        if not can_decide:
            require(request.user, "decide_proposals")
        if form.is_valid():
            d = form.cleaned_data
            try:
                decision = psvc.decide(proposal, user=request.user, outcome=d["outcome"], rationale=d["rationale"],
                                       planning_department=d["planning_department"], planner=d["planner"])
                messages.success(request, f"Decision recorded: {decision.outcome}" + (" (Founder override)" if decision.kind == "override" else ""))
                if decision.effects.get("project"):
                    return redirect("project_detail", decision.effects["project"])
            except psvc.ProposalError as exc:
                messages.error(request, str(exc))
            return redirect("proposal_detail", pk)
    project = Project.objects.filter(proposal=proposal).first()
    from apps.core.models import CompanySettings

    return render(request, "proposals/detail.html", {
        "p": proposal, "form": form, "project": project, "decisions": proposal.decisions.select_related("made_by", "overrides"),
        "ceo_limit": CompanySettings.get().ceo_approval_limit_usd, "revisions": proposal.revisions.all(),
        "retrieved_count": sum(1 for e in proposal.evidence if e.get("retrieved")),
    })


def project_list(request):
    require(request.user, "view_projects")
    qs = scope_queryset(request.user, "view_projects", Project.objects.select_related("department"), project_field="pk")
    qs = qs.annotate(n_tasks=Count("tasks", filter=~Q(tasks__status="cancelled") & ~Q(tasks__output_kind="plan"), distinct=True),
                     n_done=Count("tasks", filter=Q(tasks__status="completed") & ~Q(tasks__output_kind="plan"), distinct=True))
    return render(request, "projects/list.html", {"projects": qs})


def project_detail(request, pk):
    project = get_object_or_404(Project.objects.select_related("department", "proposal", "template"), pk=pk)
    require(request.user, "view_projects", project)
    manage = can(request.user, "manage_projects", project)
    tasks = project.tasks.select_related("assigned_employee", "milestone", "department").prefetch_related("dependencies__depends_on")
    plan_tasks = [t for t in tasks if t.output_kind != Task.PLAN]
    readiness = svc.release_readiness(project) if project.has_repository else None
    return render(request, "projects/detail.html", {
        "project": project, "manage": manage, "plan_tasks": plan_tasks,
        "planning_tasks": [t for t in tasks if t.output_kind == Task.PLAN],
        "milestones": project.milestones.all(), "decisions": project.decisions.select_related("made_by")[:30],
        "priority_form": ProjectPriorityForm(initial={"priority": project.priority}),
        "release_form": ReleaseForm(), "releases": project.releases.select_related("test_run", "approval"),
        "readiness": readiness, "test_runs": project.test_runs.all()[:10],
        "log": repo.log(project) if project.has_repository else [], "branches": repo.branches(project) if project.has_repository else [],
        "done": sum(1 for t in plan_tasks if t.status == Task.COMPLETED), "total": sum(1 for t in plan_tasks if t.status != Task.CANCELLED),
    })


@require_POST
def project_action(request, pk):
    project = get_object_or_404(Project, pk=pk)
    require(request.user, "manage_projects", project)
    action = request.POST.get("action")
    try:
        if action == "activate_plan":
            n = len(svc.activate_plan(project, user=request.user, rationale=request.POST.get("rationale", "")))
            messages.success(request, f"Plan activated: {n} tasks submitted to the engine.")
        elif action == "priority":
            form = ProjectPriorityForm(request.POST)
            if not form.is_valid():
                raise svc.ProjectError("Choose a priority and give a reason")
            fx = svc.change_project_priority(project, form.cleaned_data["priority"], user=request.user, reason=form.cleaned_data["reason"])
            messages.success(request, f"Priority updated on {fx['tasks_updated']} open tasks; {fx['running_unaffected']} running task(s) continue uninterrupted.")
        elif action in ("pause", "resume", "cancel", "complete"):
            status = {"pause": Project.PAUSED, "resume": Project.ACTIVE, "cancel": Project.CANCELLED, "complete": Project.COMPLETED}[action]
            if action == "complete" and project.has_repository and not svc.release_readiness(project)["ready"]:
                raise svc.ProjectError("Cannot mark complete: release readiness checks are not satisfied")
            svc.set_project_status(project, status, user=request.user, reason=request.POST.get("reason", ""))
            messages.success(request, f"Project {project.get_status_display().lower()}.")
        elif action == "run_tests":
            run = svc.run_release_tests(project, user=request.user)
            (messages.success if run.passed else messages.error)(
                request, f"Test run #{run.pk} on main {run.commit_sha[:10]}: {'passed' if run.passed else 'FAILED'} ({run.tests_run or 0} tests).")
        elif action == "create_release":
            form = ReleaseForm(request.POST)
            if not form.is_valid():
                raise svc.ProjectError("Version and release notes are required")
            rel = svc.create_release(project, user=request.user, **form.cleaned_data)
            messages.success(request, f"Release v{rel.version} drafted" + ("" if rel.checklist["ready"] else " (not yet ready)"))
        elif action == "request_release":
            rel = get_object_or_404(Release, pk=request.POST.get("release"), project=project)
            svc.request_release_approval(rel, user=request.user)
            messages.success(request, "Release submitted for Founder approval.")
        else:
            raise svc.ProjectError("Unknown action")
    except (svc.ProjectError, repo.RepoError) as exc:
        messages.error(request, str(exc))
    return redirect("project_detail", pk)


def project_templates(request):
    require(request.user, "view_projects")
    form = ProjectTemplateForm(request.POST or None) if can(request.user, "manage_settings") else None
    if request.method == "POST":
        require(request.user, "manage_settings")
        if form.is_valid():
            t = form.save()
            audit.record("project_template.created", user=request.user, target=t)
            messages.success(request, "Template created.")
            return redirect("project_templates")
    return render(request, "projects/templates.html", {"templates": ProjectTemplate.objects.all(), "form": form})
