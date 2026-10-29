"""Project planning, activation, priority management, status control and release readiness."""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core import audit
from apps.core.models import CompanySettings, Department
from apps.core.notify import notify_leadership

from . import repo, services
from .models import ApprovalRequest, Decision, Milestone, Priority, Project, Release, Task, TaskDependency, TestRun

T = Task
MAX_PLAN_TASKS = 50


class ProjectError(Exception):
    pass


def planning_brief(project):
    company = CompanySettings.get()
    p = project.proposal
    parts = [f"Create the delivery plan for project {project.code}: {project.name}.", f"PROBLEM:\n{project.description}"]
    if p:
        parts += [f"TARGET USERS:\n{p.target_users}", "FEATURES:\n" + "\n".join(f"- {f}" for f in p.proposed_features),
                  "RECOMMENDED STACK: " + ", ".join(p.recommended_stack), f"VALIDATION PLAN:\n{p.validation_plan}",
                  "KNOWN RISKS:\n" + "\n".join(f"- {r}" for r in p.risks)]
    if project.template:
        parts.append("TEMPLATE MILESTONES: " + ", ".join(project.template.default_milestones))
    parts.append("AVAILABLE DEPARTMENTS: " + ", ".join(Department.objects.filter(is_active=True).values_list("name", flat=True)))
    policy = [("Permitted languages", ", ".join(company.permitted_languages)), ("Permitted frameworks", ", ".join(company.permitted_frameworks)),
              ("Testing rules", company.testing_rules), ("Security requirements", company.security_requirements),
              ("Licensing policy", company.licensing_policy), ("Coding standards", company.development_standards)]
    parts.append("SOFTWARE POLICY:\n" + "\n".join(f"- {k}: {v}" for k, v in policy if v))
    if project.has_repository:
        parts.append("Implementation tasks run in a Git repository (one branch per task, merged to main after review). "
                     "Include tasks that write unittest tests (test*.py) so releases have test evidence.")
    return "\n\n".join(parts)


# ------------------------------------------------------------------ plan validation & materialization

def _has_cycle(graph):
    state = {}

    def visit(n):
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        if any(visit(m) for m in graph[n]):
            return True
        state[n] = 2
        return False

    return any(visit(n) for n in graph)


def validate_plan(project, data):
    """Validate a planner's output. Raises ValueError with a message the planner can act on."""
    if not isinstance(data, dict):
        raise ValueError("'plan' must be a JSON object with 'milestones' and 'tasks'")
    depts = {d.name.lower(): d for d in Department.objects.filter(is_active=True)}
    milestones = data.get("milestones", [])
    if not isinstance(milestones, list):
        raise ValueError("'milestones' must be a list")
    ms_clean = []
    for m in milestones[:20]:
        if not isinstance(m, dict) or not isinstance(m.get("name"), str) or not m["name"].strip():
            raise ValueError("each milestone needs a 'name'")
        days = m.get("due_in_days")
        if days is not None and (not isinstance(days, int) or days < 0 or days > 3650):
            raise ValueError("milestone 'due_in_days' must be a whole number of days")
        ms_clean.append({"name": m["name"].strip()[:200], "due_in_days": days})
    ms_names = {m["name"] for m in ms_clean}
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("'tasks' must be a non-empty list")
    if len(tasks) > MAX_PLAN_TASKS:
        raise ValueError(f"at most {MAX_PLAN_TASKS} tasks per plan")
    clean, keys = [], set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            raise ValueError(f"task #{i + 1} must be an object")
        key = t.get("key")
        if not isinstance(key, str) or not key.strip() or key in keys:
            raise ValueError(f"task #{i + 1} needs a unique string 'key'")
        keys.add(key)
        for f in ("title", "description", "acceptance_criteria"):
            if not isinstance(t.get(f), str) or not t[f].strip():
                raise ValueError(f"task '{key}' is missing '{f}'")
        dept_name = t.get("department")
        if dept_name:
            if not isinstance(dept_name, str) or dept_name.lower() not in depts:
                raise ValueError(f"task '{key}': unknown department '{dept_name}'. Use one of: {', '.join(d.name for d in depts.values())}")
            dept = depts[dept_name.lower()]
        elif project.department_id:
            dept = project.department
        else:
            raise ValueError(f"task '{key}' needs a 'department'")
        ms = t.get("milestone")
        if ms and ms not in ms_names:
            raise ValueError(f"task '{key}': milestone '{ms}' is not defined")
        deps = t.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ValueError(f"task '{key}': 'depends_on' must be a list of task keys")
        skills = t.get("skills", [])
        if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
            raise ValueError(f"task '{key}': 'skills' must be a list of strings")
        clean.append({"key": key, "title": t["title"].strip()[:200], "description": t["description"].strip()[:8000],
                      "acceptance_criteria": t["acceptance_criteria"].strip()[:4000], "department": dept, "milestone": ms or None,
                      "depends_on": deps, "skills": [s.lower()[:40] for s in skills[:10]],
                      "requires_review": t.get("requires_review", True) is not False})
    for t in clean:
        missing = [d for d in t["depends_on"] if d not in keys]
        if missing:
            raise ValueError(f"task '{t['key']}' depends on unknown keys: {', '.join(missing)}")
    if _has_cycle({t["key"]: t["depends_on"] for t in clean}):
        raise ValueError("task dependencies contain a cycle")
    return {"milestones": ms_clean, "tasks": clean}


def materialize_plan(project, plan, *, source_task=None):
    """Create milestones and draft tasks (with dependencies). Activation is a separate, authorized step."""
    company = CompanySettings.get()
    today = timezone.localdate()
    with transaction.atomic():
        milestones = {m["name"]: Milestone.objects.create(
            project=project, name=m["name"], due_date=today + timedelta(days=m["due_in_days"]) if m["due_in_days"] is not None else None)
            for m in plan["milestones"]}
        created = {}
        for t in plan["tasks"]:
            created[t["key"]] = Task.objects.create(
                title=t["title"], description=t["description"], acceptance_criteria=t["acceptance_criteria"], project=project,
                milestone=milestones.get(t["milestone"]), department=t["department"], priority=project.priority,
                required_skills=t["skills"], requires_review=t["requires_review"], created_by=project.created_by,
                max_steps=company.default_max_steps, max_retries=company.default_max_retries,
                max_runtime_seconds=company.default_max_runtime_seconds)
        for t in plan["tasks"]:
            for dep in t["depends_on"]:
                TaskDependency.objects.create(task=created[t["key"]], depends_on=created[dep])
        Project.objects.filter(pk=project.pk).update(plan_status=Project.PLAN_PROPOSED)
        project.plan_status = Project.PLAN_PROPOSED
        audit.record("project.plan_proposed", employee=getattr(source_task, "assigned_employee", None), target=project,
                     details={"tasks": len(created), "milestones": len(milestones)})
    notify_leadership("plan", f"Plan ready for {project.code}: {len(created)} tasks", project.name, f"/projects/{project.pk}/")
    return list(created.values())


def activate_plan(project, *, user, rationale=""):
    if project.plan_status != Project.PLAN_PROPOSED:
        raise ProjectError("There is no proposed plan to activate")
    drafts = list(project.tasks.filter(status=T.DRAFT).order_by("id"))
    with transaction.atomic():
        for task in drafts:
            services.queue_task(task, user=user, reason="plan activated")
        project.plan_status, project.status = Project.PLAN_ACTIVE, Project.ACTIVE
        project.save(update_fields=["plan_status", "status"])
        Decision.objects.create(kind="plan", outcome="activated", rationale=rationale, project=project, made_by=user,
                                authority="founder" if user.is_founder else "ceo", effects={"tasks_queued": len(drafts)})
        audit.record("project.plan_activated", user=user, target=project, details={"tasks": len(drafts)})
    return drafts


# ------------------------------------------------------------------ priority & status

def _queue_rank(task_id):
    ids = list(Task.objects.filter(status=T.QUEUED).exclude(priority=Priority.ON_HOLD)
               .order_by("priority", "queued_at", "id").values_list("id", flat=True))
    return ids.index(task_id) + 1 if task_id in ids else None


def change_project_priority(project, priority, *, user, reason=""):
    """Re-prioritize a project and its open tasks. Running tasks are never interrupted by a priority change."""
    if priority not in Priority.values:
        raise ProjectError("Unknown priority")
    if not reason.strip():
        raise ProjectError("A reason is required for priority changes")
    open_tasks = project.tasks.exclude(status__in=T.TERMINAL)
    first_queued = open_tasks.filter(status=T.QUEUED).order_by("queued_at", "id").first()
    rank_before = _queue_rank(first_queued.pk) if first_queued else None
    old = project.priority
    with transaction.atomic():
        project.priority = priority
        project.save(update_fields=["priority"])
        updated = open_tasks.update(priority=priority)
        running = project.tasks.filter(status=T.RUNNING).count()
        effects = {"from": old, "to": priority, "tasks_updated": updated, "running_unaffected": running,
                   "first_queued_rank_before": rank_before,
                   "first_queued_rank_after": _queue_rank(first_queued.pk) if first_queued else None,
                   "deadline": str(project.deadline) if project.deadline else None}
        if priority == Priority.ON_HOLD:
            effects["note"] = "on hold: queued tasks will not be scheduled; running tasks finish their current work"
        Decision.objects.create(kind="priority", outcome=Priority(priority).label, rationale=reason, project=project, made_by=user,
                                authority="founder" if user.is_founder else "ceo", effects=effects)
        audit.record("project.priority", user=user, target=project, details=effects | {"reason": reason[:300]})
    return effects


def set_project_status(project, status, *, user, reason=""):
    old = project.status
    if status not in dict(Project.STATUSES) or status == old:
        raise ProjectError("Invalid status change")
    if old in (Project.COMPLETED, Project.CANCELLED):
        raise ProjectError("This project is closed")
    effects = {"from": old, "to": status}
    with transaction.atomic():
        project.status = status
        project.save(update_fields=["status"])
        if status == Project.CANCELLED:
            n = 0
            for task in project.tasks.exclude(status__in=T.TERMINAL):
                try:
                    services.cancel_task(task, user=user, reason=f"project cancelled: {reason}"[:500])
                    n += 1
                except services.InvalidTransition:
                    pass
            effects["tasks_cancelled"] = n
        if status == Project.ACTIVE and old == Project.PAUSED:
            n = 0
            for task in project.tasks.filter(status=T.PAUSED, status_reason="project paused"):
                services.resume_task(task, user=user)
                n += 1
            effects["tasks_resumed"] = n
        Decision.objects.create(kind="project_status", outcome=status, rationale=reason, project=project, made_by=user,
                                authority="founder" if user.is_founder else "ceo", effects=effects)
        audit.record("project.status", user=user, target=project, details=effects)
    return effects


# ------------------------------------------------------------------ Software Factory: tests & releases

def run_release_tests(project, *, user):
    """Run the unittest suite on a clean checkout of main and record the evidence."""
    from apps.tools import testrunner

    if not project.has_repository:
        raise ProjectError("This project has no repository")
    with repo.detached_checkout(project, "main") as (path, sha):
        res = testrunner.run_tests(path)
    run = TestRun.objects.create(project=project, ref="main", commit_sha=sha, command=testrunner.COMMAND,
                                 exit_code=res["exit_code"], tests_run=res["tests_run"], failures=res["failures"],
                                 passed=res["passed"], timed_out=res["timed_out"], output=res["output"],
                                 triggered_by=f"user:{user.username}")
    audit.record("project.tests_run", user=user, target=project, details={"passed": run.passed, "sha": sha, "tests": run.tests_run})
    return run


def _review_ok(task):
    if not task.requires_review:
        return True
    if task.reviews.filter(outcome="accept").exists():
        return True
    return task.revisions.filter(status=T.COMPLETED, reviews__outcome="accept").exists()


def release_readiness(project):
    """Evidence-based checklist. Every item is computed from recorded data, never from agent claims."""
    tasks = project.tasks.exclude(status=T.CANCELLED).exclude(output_kind=T.PLAN)
    open_tasks = tasks.exclude(status=T.COMPLETED)
    unreviewed = [t.code for t in tasks.filter(status=T.COMPLETED) if not _review_ok(t)]
    main_sha = repo.log(project, n=1)[0]["sha"] if project.has_repository and repo.log(project, n=1) else ""
    latest = project.test_runs.filter(ref="main").first()
    items = [
        {"key": "repository", "label": "Project repository exists", "ok": project.has_repository, "detail": ""},
        {"key": "plan", "label": "Plan activated", "ok": project.plan_status == Project.PLAN_ACTIVE, "detail": project.get_plan_status_display()},
        {"key": "tasks", "label": "All tasks completed", "ok": tasks.exists() and not open_tasks.exists(),
         "detail": f"{open_tasks.count()} open" if open_tasks.exists() else f"{tasks.count()} completed"},
        {"key": "reviews", "label": "Every reviewed deliverable was accepted by a reviewer", "ok": not unreviewed,
         "detail": ", ".join(unreviewed[:10])},
        {"key": "tests", "label": "Test suite passed on the current main commit",
         "ok": bool(latest and latest.passed and latest.commit_sha == main_sha and main_sha),
         "detail": (f"latest run #{latest.pk}: {'passed' if latest.passed else 'failed'} on {latest.commit_sha[:10]}"
                    + ("" if latest.commit_sha == main_sha else " (main has changed since)")) if latest else "no test run on main"},
    ]
    return {"ready": all(i["ok"] for i in items), "items": items, "main_sha": main_sha, "test_run": latest}


def create_release(project, *, user, version, release_notes, known_issues="", deployment_plan=""):
    if not version.strip() or not release_notes.strip():
        raise ProjectError("Version and release notes are required")
    if project.releases.filter(version=version.strip()).exists():
        raise ProjectError("That version already exists")
    r = release_readiness(project)
    release = Release.objects.create(project=project, version=version.strip()[:40], release_notes=release_notes,
                                     known_issues=known_issues, deployment_plan=deployment_plan, commit_sha=r["main_sha"],
                                     test_run=r["test_run"], created_by=user,
                                     checklist={"ready": r["ready"], "items": r["items"], "at": timezone.now().isoformat()})
    audit.record("release.created", user=user, target=release, details={"version": release.version, "ready": r["ready"]})
    return release


def request_release_approval(release, *, user):
    if release.status not in (Release.DRAFT, Release.REJECTED):
        raise ProjectError("This release is already submitted or approved")
    r = release_readiness(release.project)
    if not r["ready"]:
        raise ProjectError("Release is not ready: " + "; ".join(i["label"] for i in r["items"] if not i["ok"]))
    if r["main_sha"] != release.commit_sha:
        raise ProjectError("main has changed since this release was drafted; create a new release")
    with transaction.atomic():
        approval = ApprovalRequest.objects.create(
            kind="deployment", required_authority="founder", project=release.project, requested_by_label=f"user:{user.username}",
            title=f"Approve release {release.project.code} v{release.version} for deployment",
            description=f"Commit {release.commit_sha}\nTest run #{release.test_run_id} passed.\n\nRELEASE NOTES:\n{release.release_notes}"
                        f"\n\nKNOWN ISSUES:\n{release.known_issues or 'none recorded'}\n\nDEPLOYMENT PLAN:\n{release.deployment_plan or 'not specified'}")
        release.status, release.approval = Release.AWAITING_APPROVAL, approval
        release.checklist = {"ready": True, "items": r["items"], "at": timezone.now().isoformat()}
        release.save()
        audit.record("release.approval_requested", user=user, target=release)
    notify_leadership("release", f"Release ready for approval: {release.project.code} v{release.version}", "", "/approvals/")
    return approval


def on_release_decision(approval, user, approved):
    release = getattr(approval, "release", None)
    if release is None:
        return
    release.status = Release.APPROVED if approved else Release.REJECTED
    release.save(update_fields=["status"])
    Decision.objects.create(kind="release", outcome=release.status, rationale=approval.decision_note, project=release.project,
                            made_by=user, authority="founder", effects={"version": release.version, "commit": release.commit_sha})
