"""Server-side Git operations for Software Factory projects.

Layout under VCOOS_WORKSPACE_ROOT/projects/project-<id>/:
    repo/                 main working tree (branch main) - only the server writes here
    worktrees/<branch>/   one isolated worktree per task branch (agents work only here)

Git always runs with hooks disabled and a minimal environment, so files written by agents
(e.g. a planted hook or config) cannot execute code or redirect operations.
"""
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

BRANCH_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,60}$")


class RepoError(Exception):
    pass


class MergeConflict(RepoError):
    pass


def git_available():
    return shutil.which("git") is not None


def project_root(project) -> Path:
    return Path(settings.VCOOS_WORKSPACE_ROOT).resolve() / "projects" / f"project-{project.pk}"


def repo_path(project) -> Path:
    return project_root(project) / "repo"


def _env(root):
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def _git(cwd, *args, check=True, author=None, timeout=60):
    cwd = Path(cwd)
    env = _env(cwd)
    if author:
        name, email = author
        env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME="VCoOS Engine",
                   GIT_COMMITTER_EMAIL="engine@vcoos.local")
    cmd = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
           "-c", "protocol.allow=never", "-c", "user.name=VCoOS Engine", "-c", "user.email=engine@vcoos.local", *args]
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RepoError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:500]}")
    return proc


def init_repo(project, readme=""):
    """Create the project repository with an initial commit on main (idempotent)."""
    if not git_available():
        raise RepoError("git is not installed on this server")
    repo = repo_path(project)
    if (repo / ".git").is_dir():
        return repo
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text(readme or f"# {project.name}\n\n{project.description}\n")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.venv/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"{project.code}: initialize repository")
    return repo


def branch_for(task):
    if task.branch_name:
        return task.branch_name
    root = task
    while root.parent_task_id:  # revisions continue on the original task's branch
        root = root.parent_task
    return root.branch_name or f"task-{root.pk}"


def worktree_path(project, branch) -> Path:
    if not BRANCH_RE.match(branch):
        raise RepoError("invalid branch name")
    return project_root(project) / "worktrees" / branch


def verify_worktree(project, path):
    """Ensure the worktree's .git pointer still references this project's repository."""
    git_file = Path(path) / ".git"
    if not git_file.is_file() or git_file.is_symlink():
        raise RepoError("worktree integrity check failed: .git is not a pointer file")
    content = git_file.read_text().strip()
    if not content.startswith("gitdir: "):
        raise RepoError("worktree integrity check failed")
    target = Path(content[len("gitdir: "):]).resolve()
    expected = (repo_path(project) / ".git" / "worktrees").resolve()
    if expected not in target.parents:
        raise RepoError("worktree integrity check failed: points outside the project repository")


def ensure_worktree(task):
    project = task.project
    branch = branch_for(task)
    if task.branch_name != branch:
        type(task).objects.filter(pk=task.pk).update(branch_name=branch)
        task.branch_name = branch
    path = worktree_path(project, branch)
    repo = init_repo(project)
    if path.exists():
        verify_worktree(project, path)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
    if exists:
        _git(repo, "worktree", "add", "-q", str(path), branch)
    else:
        _git(repo, "worktree", "add", "-q", "-b", branch, str(path), "main")
    return path


def head(path):
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def is_dirty(path):
    return bool(_git(path, "status", "--porcelain").stdout.strip())


def status(path):
    out = _git(path, "status", "--porcelain=v1", "--branch").stdout
    return out[:8000]


def diff_against_main(path, limit=20000):
    _git(path, "add", "-A", "--intent-to-add")
    out = _git(path, "diff", "main", "--stat", "--patch").stdout
    return out[:limit] + ("\n[diff truncated]" if len(out) > limit else "")


def commit_all(path, message, employee=None):
    """Commit every change in a worktree. Returns the new sha, or None if nothing changed."""
    if not is_dirty(path):
        return None
    author = (employee.name, f"{employee.employee_id.lower()}@agents.vcoos.local") if employee else None
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", message[:500], author=author)
    return head(path)


def merge_branch(project, branch, message):
    """Merge a task branch into main. On conflict the merge is aborted and MergeConflict raised."""
    repo = repo_path(project)
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode != 0:
        raise RepoError(f"branch {branch} does not exist")
    proc = _git(repo, "merge", "--no-ff", "-m", message[:500], branch, check=False)
    if proc.returncode != 0:
        _git(repo, "merge", "--abort", check=False)
        raise MergeConflict(f"{branch} conflicts with main: {(proc.stdout + proc.stderr).strip()[:400]}")
    return head(repo)


def log(project, ref="main", n=20):
    repo = repo_path(project)
    if not (repo / ".git").is_dir():
        return []
    out = _git(repo, "log", ref, f"-n{n}", "--date=iso", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s", check=False).stdout
    return [dict(zip(("sha", "author", "date", "subject"), line.split("\x1f"))) for line in out.splitlines() if line]


def branches(project):
    repo = repo_path(project)
    if not (repo / ".git").is_dir():
        return []
    out = _git(repo, "branch", "--format=%(refname:short)%x1f%(objectname:short)%x1f%(subject)").stdout
    return [dict(zip(("name", "sha", "subject"), line.split("\x1f"))) for line in out.splitlines() if line]


@contextmanager
def detached_checkout(project, ref="main"):
    """Temporary clean checkout of `ref` (used for release test runs)."""
    repo = repo_path(project)
    path = project_root(project) / "checkouts" / f"{ref}-{uuid.uuid4().hex[:8]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(path), ref)
    try:
        yield path, head(path)
    finally:
        _git(repo, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
