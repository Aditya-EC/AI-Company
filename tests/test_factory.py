"""Phase 6: research proposals, CEO evaluation and Founder override, planning, project priority,
Software Factory repositories, test evidence and release governance."""
import json
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.core.models import CompanySettings, Department, EmployeeProfile, PermissionAssignment
from apps.gateway.models import EmployeeModelPolicy
from apps.orchestration import engine
from apps.tools import testrunner, webfetch
from apps.work import projects as psvc
from apps.work import proposals, repo, services
from apps.work.models import (ApprovalRequest, Decision, Priority, Project, ProjectProposal, Release, Task, TaskDependency,
                              TestRun, ToolCall)

from .conftest import PASSWORD, grant, make_task, no_sleep

W = "factory-worker"


def run_task(task):
    if task.status != Task.QUEUED:
        services.queue_task(task)
    assert engine.claim_task(task.pk, W), task.status_reason
    engine.execute_task(task.pk, W, sleep=no_sleep)
    task.refresh_from_db()
    return task


def good_proposal(**over):
    p = {"title": "AI tutor for engineering students", "problem_statement": "Students lack affordable feedback.",
         "target_users": "Engineering undergraduates", "technical_feasibility": "Feasible with local models.",
         "validation_plan": "Pilot with 20 students.", "proposed_features": ["Problem feedback", "Progress tracking"],
         "risks": ["Model accuracy"], "assumptions": ["Students will pay"], "uncertainties": ["Market size"],
         "recommended_stack": ["Python", "Django"], "estimated_effort_days": 30, "estimated_cost_usd": 400,
         "confidence": 60, "software_category": "web_app", "evidence": []}
    p.update(over)
    return p


def final_json(output="done", **extra):
    return json.dumps({"action": "final", "output": output, "summary": "ok", **extra})


@pytest.fixture
def seeded(db):
    from django.core.management import call_command
    call_command("seed_vcoos")


@pytest.fixture
def proposal(db, dept, employee):
    return ProjectProposal.objects.create(**{k: v for k, v in proposals.validate_payload(good_proposal()).items()},
                                          department=dept, proposed_by=employee, status=ProjectProposal.SUBMITTED)


# ------------------------------------------------------------------ proposal validation & research

def test_proposal_payload_validation():
    assert proposals.validate_payload(good_proposal())["confidence"] == 60
    for bad, msg in [({"title": ""}, "title"), ({"proposed_features": []}, "proposed_features"), ({"confidence": 150}, "confidence"),
                     ({"evidence": [{"claim": "x", "source_url": "javascript:alert(1)"}]}, "source_url"),
                     ({"estimated_effort_days": "lots"}, "estimated_effort_days")]:
        with pytest.raises(ValueError, match=msg):
            proposals.validate_payload(good_proposal(**bad))
    data = good_proposal()
    del data["assumptions"]
    with pytest.raises(ValueError, match="assumptions"):
        proposals.validate_payload(data)


def test_research_task_produces_proposal_with_retrieval_tracking(llm, employee, dept, company, tools, founder, settings):
    settings.VCOOS_WEB_FETCH_ALLOW_PRIVATE = True  # the test page is served on localhost
    grant(tools["web.fetch"], employee=employee)
    task = proposals.commission_research(brief="AI tutoring", department=dept, employee=employee, user=founder)
    page = f"{llm.url}/page"
    llm.script = [
        llm.ollama(llm.tool("web.fetch", {"url": page})),
        llm.ollama(final_json(proposal={"title": "incomplete"})),  # invalid -> feedback, same model retries
        llm.ollama(final_json(proposal=good_proposal(evidence=[
            {"claim": "Market grew 12%", "source_url": page},
            {"claim": "Competitor X has 1M users", "source_url": "https://example.com/never-fetched"}]))),
    ]
    task = run_task(task)
    assert task.status == Task.COMPLETED
    fetched = ToolCall.objects.get(tool_slug="web.fetch")
    assert fetched.success and "Tutoring market grew 12%" in fetched.output_summary and "steal()" not in fetched.output_summary
    assert "rejected by validation" in llm.requests[2]["json"]["messages"][-1]["content"]
    p = ProjectProposal.objects.get(source_task=task)
    assert p.status == ProjectProposal.SUBMITTED and p.proposed_by == employee
    assert [e["retrieved"] for e in p.evidence] == [True, False]  # system-observed, not agent-claimed
    assert [s["action"] for s in task.executions.get().steps] == ["tool", "final_rejected", "final"]


def test_web_fetch_ssrf_protection(db, company, llm):
    for url in ["http://127.0.0.1:8000/", "http://localhost/", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd",
                "ftp://example.com/", "http://user:pw@example.com/", "http://10.0.0.5/"]:
        with pytest.raises(webfetch.FetchError):
            webfetch.check_url(url)
    company.web_allowed_domains = ["example.org"]
    company.save()
    with pytest.raises(webfetch.FetchError, match="allowed list"):
        webfetch.check_url("https://example.com/")


def test_web_fetch_redirect_to_internal_blocked(db, company, llm, settings, monkeypatch):
    settings.VCOOS_WEB_FETCH_ALLOW_PRIVATE = False
    monkeypatch.setattr(webfetch, "_is_public", lambda host: host == "127.0.0.1")  # treat only the test server as public
    with pytest.raises(webfetch.FetchError, match="not a public address"):
        webfetch.fetch(f"{llm.url}/redirect-internal")


# ------------------------------------------------------------------ CEO evaluation & Founder override

def test_ceo_approves_within_limit_creates_project_repo_and_planning_task(seeded, ceo, proposal, company, dept):
    company.ceo_approval_limit_usd = Decimal("500")
    company.save()
    d = proposals.decide(proposal, user=ceo, outcome="approve", rationale="good fit")
    project = Project.objects.get(proposal=proposal)
    assert d.authority == "ceo" and d.effects["project"] == project.pk
    assert project.has_repository and repo.log(project)[0]["subject"].endswith("initialize repository")
    plan_task = Task.objects.get(pk=d.effects["planning_task"])
    assert plan_task.output_kind == Task.PLAN and plan_task.status == Task.QUEUED and plan_task.project == project
    assert "Engineering" in plan_task.description  # available departments are given to the planner


def test_ceo_authority_limits(ceo, proposal, company):
    company.ceo_approval_limit_usd = Decimal("100")
    company.save()
    with pytest.raises(proposals.ProposalError, match="exceeds the CEO approval limit"):
        proposals.decide(proposal, user=ceo, outcome="approve")
    proposal.estimated_cost_usd = None
    proposal.save()
    with pytest.raises(proposals.ProposalError, match="No cost estimate"):
        proposals.decide(proposal, user=ceo, outcome="approve")
    with pytest.raises(proposals.ProposalError, match="rationale"):
        proposals.decide(proposal, user=ceo, outcome="escalate")
    proposals.decide(proposal, user=ceo, outcome="escalate", rationale="cost unknown")
    with pytest.raises(proposals.ProposalError, match="Only the Founder"):
        proposals.decide(proposal, user=ceo, outcome="reject", rationale="no")


def test_staff_cannot_decide(staff, proposal):
    PermissionAssignment.objects.create(user=staff, permission="manage_projects")
    with pytest.raises(proposals.ProposalError):
        proposals.decide(proposal, user=staff, outcome="approve")


def test_founder_overrides_ceo_rejection_then_reversal_cancels_project(seeded, founder, ceo, proposal, company):
    first = proposals.decide(proposal, user=ceo, outcome="reject", rationale="not now")
    override = proposals.decide(proposal, user=founder, outcome="approve", rationale="strategic priority")
    assert override.kind == "override" and override.overrides == first and override.authority == "founder"
    project = Project.objects.get(proposal=proposal)
    reversal = proposals.decide(proposal, user=founder, outcome="reject", rationale="changed strategy")
    project.refresh_from_db()
    assert project.status == Project.CANCELLED and reversal.effects["project_cancelled"] == project.pk
    assert all(t.status == Task.CANCELLED for t in project.tasks.all())


def test_category_policy_enforced(founder, proposal, company):
    company.permitted_software_categories = ["api"]
    company.save()
    with pytest.raises(proposals.ProposalError, match="not permitted"):
        proposals.decide(proposal, user=founder, outcome="approve")


def test_revision_request_creates_research_revision(llm, founder, employee, dept, company, ceo):
    task = proposals.commission_research(brief="x", department=dept, employee=employee, user=founder)
    llm.script = [llm.ollama(final_json(proposal=good_proposal()))]
    run_task(task)
    p1 = ProjectProposal.objects.get(source_task=task)
    d = proposals.decide(p1, user=ceo, outcome="revision", rationale="Add competitor pricing")
    rev = Task.objects.get(pk=d.effects["revision_task"])
    assert rev.parent_task == task and "Add competitor pricing" in rev.description and rev.output_kind == Task.PROPOSAL
    llm.script = [llm.ollama(final_json(proposal=good_proposal(title="AI tutor v2")))]
    run_task(rev)
    p2 = ProjectProposal.objects.get(source_task=rev)
    assert p2.revision_of == p1


# ------------------------------------------------------------------ planning

def plan_payload(dept_name="Engineering"):
    return {"milestones": [{"name": "MVP", "due_in_days": 14}],
            "tasks": [{"key": "model", "title": "Data model", "description": "Create models", "acceptance_criteria": "Migrations run",
                       "department": dept_name, "milestone": "MVP", "skills": ["python"]},
                      {"key": "api", "title": "API", "description": "Endpoints", "acceptance_criteria": "Tests pass",
                       "department": dept_name, "milestone": "MVP", "depends_on": ["model"]}]}


def test_plan_validation_errors(db, dept):
    project = Project.objects.create(name="X", department=dept)
    assert len(psvc.validate_plan(project, plan_payload())["tasks"]) == 2
    bad_dept = plan_payload("Nonexistent")
    with pytest.raises(ValueError, match="unknown department"):
        psvc.validate_plan(project, bad_dept)
    cyc = plan_payload()
    cyc["tasks"][0]["depends_on"] = ["api"]
    with pytest.raises(ValueError, match="cycle"):
        psvc.validate_plan(project, cyc)
    unknown = plan_payload()
    unknown["tasks"][1]["depends_on"] = ["ghost"]
    with pytest.raises(ValueError, match="unknown keys"):
        psvc.validate_plan(project, unknown)
    no_ac = plan_payload()
    no_ac["tasks"][0]["acceptance_criteria"] = ""
    with pytest.raises(ValueError, match="acceptance_criteria"):
        psvc.validate_plan(project, no_ac)


def test_planning_task_materializes_plan_and_activation_queues(seeded, llm, founder, ceo, proposal, employee, company):
    d = proposals.decide(proposal, user=founder, outcome="approve", planner=employee)
    project = Project.objects.get(pk=d.effects["project"])
    plan_task = Task.objects.get(pk=d.effects["planning_task"])
    llm.script = [llm.ollama(final_json(plan=plan_payload()))]
    run_task(plan_task)
    project.refresh_from_db()
    assert plan_task.status == Task.COMPLETED and project.plan_status == Project.PLAN_PROPOSED
    model_t, api_t = project.tasks.exclude(output_kind=Task.PLAN).order_by("id")
    assert model_t.status == api_t.status == Task.DRAFT and api_t.dependencies.get().depends_on == model_t
    assert model_t.milestone.name == "MVP" and model_t.required_skills == ["python"]
    psvc.activate_plan(project, user=ceo, rationale="go")
    model_t.refresh_from_db()
    api_t.refresh_from_db()
    assert model_t.status == Task.QUEUED and api_t.status == Task.BLOCKED
    assert Decision.objects.filter(project=project, kind="plan", outcome="activated").exists()
    with pytest.raises(psvc.ProjectError):
        psvc.activate_plan(project, user=ceo)


# ------------------------------------------------------------------ priority & status

def test_project_priority_cascade_records_effects(db, dept, employee, ceo, company):
    project = Project.objects.create(name="P", department=dept)
    a = make_task(dept, employee, project=project)
    b = make_task(dept, employee, project=project)
    other = make_task(dept, employee, priority=Priority.HIGH)
    for t in (a, b, other):
        services.queue_task(t)
    engine.claim_task(a.pk, W)  # a is running
    with pytest.raises(psvc.ProjectError, match="reason"):
        psvc.change_project_priority(project, Priority.CRITICAL, user=ceo)
    fx = psvc.change_project_priority(project, Priority.CRITICAL, user=ceo, reason="customer escalation")
    a.refresh_from_db()
    assert fx["tasks_updated"] == 2 and fx["running_unaffected"] == 1 and a.status == Task.RUNNING  # not interrupted
    assert fx["first_queued_rank_before"] == 2 and fx["first_queued_rank_after"] == 1
    assert engine.next_queued_ids(5)[0] == b.pk
    d = Decision.objects.get(kind="priority")
    assert d.rationale == "customer escalation" and d.made_by == ceo


def test_project_pause_stops_at_checkpoint_and_resume_requeues(llm, dept, employee, founder, company, tools, monkeypatch):
    from apps.tools import registry
    project = Project.objects.create(name="P", department=dept)
    grant(tools["workspace.list_files"], employee=employee)
    task = make_task(dept, employee, project=project)
    original = registry.HANDLERS["workspace.list_files"]
    monkeypatch.setitem(registry.HANDLERS, "workspace.list_files",
                        lambda ctx, **kw: (Project.objects.filter(pk=project.pk).update(status=Project.PAUSED), original(ctx, **kw))[1])
    llm.script = [llm.ollama(llm.tool("workspace.list_files", {}))]
    task = run_task(task)
    assert task.status == Task.PAUSED and task.status_reason == "project paused"
    queued = make_task(dept, employee, project=project)
    services.queue_task(queued)
    assert engine.claim_task(queued.pk, W) is None
    project.refresh_from_db()
    fx = psvc.set_project_status(project, Project.ACTIVE, user=founder)
    task.refresh_from_db()
    assert fx["tasks_resumed"] == 1 and task.status == Task.QUEUED


# ------------------------------------------------------------------ Software Factory

@pytest.fixture
def repo_project(db, dept):
    p = Project.objects.create(name="Calc", department=dept, has_repository=True, status=Project.ACTIVE, plan_status=Project.PLAN_ACTIVE)
    repo.init_repo(p)
    return p


CODE = "def add(a, b):\n    return a + b\n"
TESTS = "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"


def test_branch_work_tests_commit_and_merge_on_accept(llm, repo_project, dept, employee, founder, company, tools):
    for slug in ("workspace.write_file", "tests.run", "git.diff"):
        grant(tools[slug], employee=employee)
    task = make_task(dept, employee, project=repo_project, created_by=founder, max_steps=6)
    llm.script = [llm.ollama(llm.tool("workspace.write_file", {"path": "calc.py", "content": CODE})),
                  llm.ollama(llm.tool("workspace.write_file", {"path": "test_calc.py", "content": TESTS})),
                  llm.ollama(llm.tool("tests.run", {})),
                  llm.ollama(llm.tool("git.diff", {})),
                  llm.ollama(final_json("Implemented add() with tests"))]
    task = run_task(task)
    assert task.status == Task.UNDER_REVIEW and task.branch_name == f"task-{task.pk}"
    run = TestRun.objects.get(task=task)
    assert run.passed and run.tests_run == 1 and run.dirty  # ran before the deliverable commit
    diff = ToolCall.objects.get(tool_slug="git.diff").output_summary
    assert "calc.py" in diff
    commit = task.artifacts.get(kind="commit")
    assert repo.branches(repo_project) and "calc.py" not in [c["subject"] for c in repo.log(repo_project)]
    assert not (repo.repo_path(repo_project) / "calc.py").exists()  # nothing reaches main before review
    services.review_task(task, user=founder, outcome="accept")
    task.refresh_from_db()
    assert task.status == Task.COMPLETED and "merged to main" in task.status_reason
    assert (repo.repo_path(repo_project) / "calc.py").read_text() == CODE
    assert commit.content in [c["sha"] for c in repo.log(repo_project, n=5)]


def test_merge_conflict_blocks_acceptance(llm, repo_project, dept, employee, founder, company, tools):
    grant(tools["workspace.write_file"], employee=employee)
    t1 = make_task(dept, employee, project=repo_project, created_by=founder)
    t2 = make_task(dept, employee, project=repo_project, created_by=founder)
    for t, content in ((t1, "v = 1\n"), (t2, "v = 2\n")):
        llm.script = [llm.ollama(llm.tool("workspace.write_file", {"path": "conf.py", "content": content})), llm.ollama(final_json())]
        run_task(t)
    services.review_task(t1, user=founder, outcome="accept")
    t2.refresh_from_db()
    with pytest.raises(services.InvalidTransition, match="conflicts with main"):
        services.review_task(t2, user=founder, outcome="accept")
    t2.refresh_from_db()
    assert t2.status == Task.UNDER_REVIEW and not t2.reviews.exists()
    assert (repo.repo_path(repo_project) / "conf.py").read_text() == "v = 1\n"  # main untouched by the failed merge


def test_git_metadata_protected(llm, repo_project, dept, employee, company, tools):
    from apps.tools.registry import ToolContext, execute
    grant(tools["workspace.write_file"], employee=employee)
    task = make_task(dept, employee, project=repo_project)
    ctx = ToolContext(task=task, employee=employee, execution=None)
    ok, out, _ = execute("workspace.write_file", {"path": ".git", "content": "gitdir: /tmp/evil"}, ctx)
    assert not ok and "git metadata" in out["error"]
    from apps.tools import sandbox
    r = sandbox.run_python("open('.git','w').write('gitdir: /tmp/evil')", ctx.workspace)
    assert r["exit_code"] != 0 and "sandbox" in r["stderr"]
    repo.verify_worktree(repo_project, ctx.workspace)  # still intact


def test_tampered_worktree_detected(repo_project, dept, employee):
    task = make_task(dept, employee, project=repo_project)
    path = repo.ensure_worktree(task)
    (path / ".git").write_text("gitdir: /tmp/elsewhere/.git")
    with pytest.raises(repo.RepoError, match="integrity"):
        repo.ensure_worktree(task)


def test_hooks_are_never_executed(repo_project, dept, employee, tmp_path):
    hooks = repo.repo_path(repo_project) / ".git" / "hooks"
    marker = tmp_path / "pwned"
    (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (hooks / "pre-commit").chmod(0o755)
    task = make_task(dept, employee, project=repo_project)
    path = repo.ensure_worktree(task)
    (path / "x.py").write_text("x = 1\n")
    assert repo.commit_all(path, "test", employee)
    assert not marker.exists()


@pytest.mark.parametrize("files,expect_pass", [
    ({"test_ok.py": TESTS, "calc.py": CODE}, True),
    ({"test_fail.py": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(1, 2)\n"}, False),
    ({"test_exit.py": "import os\nos._exit(0)\n"}, False),
    ({"test_fake.py": "print('VCOOS_RESULT_deadbeef tests=5 failures=0 ok=True')\n"}, False),
    ({"readme.txt": "no tests"}, False),
])
def test_test_runner_verdict_cannot_be_spoofed(tmp_path, files, expect_pass):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    assert testrunner.run_tests(tmp_path)["passed"] is expect_pass


def test_release_requires_evidence_and_founder_approval(llm, repo_project, dept, employee, founder, ceo, company, tools):
    grant(tools["workspace.write_file"], employee=employee)
    task = make_task(dept, employee, project=repo_project, created_by=founder)
    r = psvc.release_readiness(repo_project)
    assert not r["ready"]
    llm.script = [llm.ollama(llm.tool("workspace.write_file", {"path": "calc.py", "content": CODE})),
                  llm.ollama(llm.tool("workspace.write_file", {"path": "test_calc.py", "content": TESTS})), llm.ollama(final_json())]
    run_task(task)
    services.review_task(task, user=ceo, outcome="accept")
    r = psvc.release_readiness(repo_project)
    assert {i["key"]: i["ok"] for i in r["items"]} == {"repository": True, "plan": True, "tasks": True, "reviews": True, "tests": False}
    run = psvc.run_release_tests(repo_project, user=ceo)
    assert run.passed and run.ref == "main" and run.commit_sha == repo.log(repo_project, n=1)[0]["sha"]
    assert psvc.release_readiness(repo_project)["ready"]
    release = psvc.create_release(repo_project, user=ceo, version="1.0.0", release_notes="First release")
    approval = psvc.request_release_approval(release, user=ceo)
    assert approval.required_authority == "founder" and approval.kind == "deployment"
    with pytest.raises(services.InvalidTransition, match="Founder"):
        services.decide_approval(approval, user=ceo, approve=True)
    services.decide_approval(approval, user=founder, approve=True, note="ship it")
    release.refresh_from_db()
    assert release.status == Release.APPROVED
    assert Decision.objects.filter(kind="release", project=repo_project, outcome="approved").exists()


def test_release_blocked_when_main_changes_or_tests_stale(llm, repo_project, dept, employee, founder, ceo, company, tools):
    grant(tools["workspace.write_file"], employee=employee)
    task = make_task(dept, employee, project=repo_project, requires_review=False)
    (repo.repo_path(repo_project) / "calc.py").write_text(CODE)
    (repo.repo_path(repo_project) / "test_calc.py").write_text(TESTS)
    repo._git(repo.repo_path(repo_project), "add", "-A")
    repo._git(repo.repo_path(repo_project), "commit", "-q", "-m", "code")
    Task.objects.filter(pk=task.pk).update(status=Task.COMPLETED)
    psvc.run_release_tests(repo_project, user=ceo)
    release = psvc.create_release(repo_project, user=ceo, version="1.0", release_notes="n")
    (repo.repo_path(repo_project) / "extra.py").write_text("y = 2\n")
    repo._git(repo.repo_path(repo_project), "add", "-A")
    repo._git(repo.repo_path(repo_project), "commit", "-q", "-m", "late change")
    with pytest.raises(psvc.ProjectError, match="Test suite passed on the current main commit"):
        psvc.request_release_approval(release, user=ceo)


def test_failing_tests_block_release(repo_project, ceo, dept, employee):
    (repo.repo_path(repo_project) / "test_bad.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.fail('broken')\n")
    repo._git(repo.repo_path(repo_project), "add", "-A")
    repo._git(repo.repo_path(repo_project), "commit", "-q", "-m", "bad test")
    run = psvc.run_release_tests(repo_project, user=ceo)
    assert not run.passed and run.failures == 1 and "broken" in run.output


# ------------------------------------------------------------------ pages & permissions

def test_factory_pages_render_and_permissions(client, seeded, founder, ceo, staff, proposal, repo_project):
    client.login(username="founder", password=PASSWORD)
    for url in [reverse("proposal_board"), reverse("proposal_create"), reverse("proposal_detail", args=[proposal.pk]),
                reverse("project_list"), reverse("project_detail", args=[repo_project.pk]), reverse("project_templates")]:
        assert client.get(url).status_code == 200, url
    r = client.post(reverse("proposal_board"), {"action": "commission", "brief": "Find SaaS gaps", "department": repo_project.department_id})
    assert r.status_code == 302 and Task.objects.filter(output_kind=Task.PROPOSAL).exists()
    client.logout()
    client.login(username="ceo", password=PASSWORD)
    assert client.post(reverse("project_templates"), {"category": "x", "name": "X"}).status_code == 403
    r = client.post(reverse("proposal_detail", args=[proposal.pk]), {"outcome": "defer", "rationale": ""})
    proposal.refresh_from_db()
    assert proposal.status == ProjectProposal.DEFERRED
    client.logout()
    client.login(username="staff", password=PASSWORD)
    assert client.get(reverse("proposal_board")).status_code == 403
    assert client.get(reverse("project_detail", args=[repo_project.pk])).status_code == 403
    PermissionAssignment.objects.create(user=staff, permission="view_projects", department=repo_project.department)
    assert client.get(reverse("project_detail", args=[repo_project.pk])).status_code == 200
    assert client.post(reverse("project_action", args=[repo_project.pk]), {"action": "pause"}).status_code == 403
    assert client.post(reverse("proposal_detail", args=[proposal.pk]), {"outcome": "approve"}).status_code == 403
