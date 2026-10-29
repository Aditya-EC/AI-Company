from decimal import Decimal

import pytest

from apps.core.models import CompanySettings, Department, EmployeeProfile, User
from apps.gateway.models import AIModel, EmployeeModelPolicy, ModelProvider
from apps.tools.models import ToolDefinition, ToolPermission
from apps.tools.registry import sync_definitions
from apps.work.models import Task

from .fake_llm import FakeLLMServer

PASSWORD = "Str0ng-Test-Passw0rd!"


@pytest.fixture(autouse=True)
def clear_cache():
    """Rate-limit counters live in the cache; never let them leak between tests."""
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def isolated_workspace(settings, tmp_path):
    """Fresh workspace root per test (SQLite reuses primary keys after rollback)."""
    settings.VCOOS_WORKSPACE_ROOT = tmp_path / "workspaces"


@pytest.fixture
def llm():
    server = FakeLLMServer()
    yield server
    server.close()


@pytest.fixture
def company(db):
    c = CompanySettings.get()
    c.name = "Test Co"
    c.max_concurrent_tasks = 4
    c.save()
    return c


@pytest.fixture
def founder(db):
    return User.objects.create_user("founder", "f@example.com", PASSWORD, account_type=User.FOUNDER)


@pytest.fixture
def ceo(db):
    return User.objects.create_user("ceo", "c@example.com", PASSWORD, account_type=User.CEO)


@pytest.fixture
def staff(db):
    return User.objects.create_user("staff", "s@example.com", PASSWORD, account_type=User.STAFF)


@pytest.fixture
def dept(db):
    return Department.objects.create(name="Engineering", slug="engineering")


@pytest.fixture
def other_dept(db):
    return Department.objects.create(name="Finance", slug="finance")


@pytest.fixture
def tools(db):
    sync_definitions()
    return {t.slug: t for t in ToolDefinition.objects.all()}


def make_provider(name, url, kind="ollama", **kw):
    return ModelProvider.objects.create(name=name, kind=kind, base_url=url, is_local=kind == "ollama", **kw)


def make_model(provider, ident="fake-model:latest", **kw):
    defaults = dict(display_name=ident, supports_json=True, is_approved=True, max_output_tokens=256)
    defaults.update(kw)
    return AIModel.objects.create(provider=provider, model_identifier=ident, **defaults)


@pytest.fixture
def local_model(db, llm):
    return make_model(make_provider("Local Ollama", llm.url))


@pytest.fixture
def employee(db, dept, local_model):
    emp = EmployeeProfile.objects.create(name="Ada", job_title="Python Developer", department=dept, skills=["python"])
    EmployeeModelPolicy.objects.create(employee=emp, model=local_model, priority=1)
    return emp


def grant(tool, **kw):
    scope = "employee" if "employee" in kw else "department" if "department" in kw else "task" if "task" in kw else "company"
    return ToolPermission.objects.create(tool=tool, scope=scope, **kw)


def make_task(dept, employee=None, **kw):
    defaults = dict(title="Write a function", description="Write an add(a, b) function.", acceptance_criteria="Returns a+b.",
                    department=dept, assigned_employee=employee, max_retries=0, max_steps=4)
    defaults.update(kw)
    return Task.objects.create(**defaults)


def no_sleep(_):
    return None
