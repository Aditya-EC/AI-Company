"""Tool registry: handler implementations, schema validation and permission enforcement."""
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db.models import Q

from apps.core.models import CompanySettings
from apps.core.redaction import redact_text

from . import sandbox
from .models import ToolDefinition, ToolPermission

MAX_FILE_BYTES = 256 * 1024


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    task: object
    employee: object
    execution: object

    @property
    def workspace(self) -> Path:
        root = Path(settings.VCOOS_WORKSPACE_ROOT).resolve()
        ws = (root / f"task-{self.task.pk}").resolve()
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def safe_path(self, rel):
        if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or "\x00" in rel:
            raise ToolError("path must be a relative path inside the task workspace")
        p = (self.workspace / rel).resolve()
        if p != self.workspace and self.workspace not in p.parents:
            raise ToolError("path escapes the task workspace")
        return p


HANDLERS = {}
SPECS = {}


def tool(slug, name, description, input_schema, output_schema, risk="low", timeout=30, limits=None, errors=""):
    def deco(fn):
        HANDLERS[slug] = fn
        SPECS[slug] = dict(name=name, description=description, input_schema=input_schema, output_schema=output_schema,
                           risk_level=risk, timeout_seconds=timeout, resource_limits=limits or {}, error_behavior=errors)
        return fn
    return deco


def _str(desc):
    return {"type": "string", "description": desc}


@tool("workspace.write_file", "Write workspace file", "Create or overwrite a UTF-8 text file in this task's isolated workspace.",
      {"type": "object", "required": ["path", "content"], "properties": {"path": _str("relative path"), "content": _str("file content")}},
      {"type": "object", "properties": {"path": _str(""), "bytes": {"type": "integer"}, "sha256": _str("")}},
      risk="low", limits={"max_bytes": MAX_FILE_BYTES}, errors="Rejects absolute paths, traversal and oversize content.")
def write_file(ctx, path, content):
    data = content.encode()
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"content exceeds {MAX_FILE_BYTES} bytes")
    p = ctx.safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


@tool("workspace.read_file", "Read workspace file", "Read a UTF-8 text file from this task's workspace.",
      {"type": "object", "required": ["path"], "properties": {"path": _str("relative path")}},
      {"type": "object", "properties": {"content": _str("")}}, errors="Missing files return an error.")
def read_file(ctx, path):
    p = ctx.safe_path(path)
    if not p.is_file():
        raise ToolError("file not found")
    return {"content": p.read_bytes()[:MAX_FILE_BYTES].decode(errors="replace")}


@tool("workspace.list_files", "List workspace files", "List files in this task's workspace.",
      {"type": "object", "properties": {}}, {"type": "object", "properties": {"files": {"type": "array"}}})
def list_files(ctx):
    ws = ctx.workspace
    return {"files": sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file())[:500]}


@tool("python.run", "Run Python (sandboxed)", "Execute a Python 3 snippet in an isolated sandbox inside the task workspace. "
      "No network, no access outside the workspace, limited CPU/memory/time. Returns stdout/stderr/exit code.",
      {"type": "object", "required": ["code"], "properties": {"code": _str("python source")}},
      {"type": "object", "properties": {"exit_code": {"type": "integer"}, "stdout": _str(""), "stderr": _str(""), "timed_out": {"type": "boolean"}}},
      risk="medium", timeout=20, limits={"cpu_seconds": 10, "memory_mb": 256, "wall_seconds": 20, "output_kb": 64},
      errors="Timeouts and policy violations are reported in the result, not raised.")
def python_run(ctx, code):
    return sandbox.run_python(code, ctx.workspace, wall_seconds=20)


def _validate(schema, data):
    if not isinstance(data, dict):
        raise ToolError("tool input must be a JSON object")
    for key in schema.get("required", []):
        if key not in data:
            raise ToolError(f"missing required field: {key}")
    props = schema.get("properties", {})
    unknown = set(data) - set(props)
    if unknown:
        raise ToolError(f"unknown fields: {', '.join(sorted(unknown))}")
    for key, val in data.items():
        if props[key].get("type") == "string" and not isinstance(val, str):
            raise ToolError(f"{key} must be a string")


def sync_definitions():
    """Create/update ToolDefinition rows for implemented handlers (keeps Founder's enabled flag)."""
    for slug, spec in SPECS.items():
        obj, created = ToolDefinition.objects.get_or_create(slug=slug, defaults={**spec, "has_handler": True})
        if not created:
            for k, v in spec.items():
                setattr(obj, k, v)
            obj.has_handler = True
            obj.save()
    ToolDefinition.objects.exclude(slug__in=SPECS.keys()).update(has_handler=False)


def check_permission(slug, employee, task):
    """Default deny. Returns (allowed, reason)."""
    if CompanySettings.get().emergency_stop:
        return False, "emergency stop active"
    tool_def = ToolDefinition.objects.filter(slug=slug).first()
    if tool_def is None or not tool_def.has_handler:
        return False, "unknown tool"
    if not tool_def.is_enabled:
        return False, "tool disabled"
    scope = Q(scope=ToolPermission.COMPANY) | Q(scope=ToolPermission.DEPARTMENT, department_id=employee.department_id) | \
        Q(scope=ToolPermission.EMPLOYEE, employee_id=employee.pk) | Q(scope=ToolPermission.TASK, task_id=task.pk)
    if task.project_id:
        scope |= Q(scope=ToolPermission.PROJECT, project_id=task.project_id)
    grants = ToolPermission.objects.filter(scope, tool=tool_def)
    if grants.filter(allow=False).exists():
        return False, "explicitly denied"
    if grants.filter(allow=True).exists():
        return True, "granted"
    return False, "not granted to this employee"


def permitted_tools(employee, task):
    return [t for t in ToolDefinition.objects.filter(is_enabled=True, has_handler=True) if check_permission(t.slug, employee, task)[0]]


def execute(slug, data, ctx):
    """Run a permitted tool. Caller must have checked permissions. Returns (ok, result_dict, duration_ms)."""
    allowed, reason = check_permission(slug, ctx.employee, ctx.task)
    if not allowed:  # defense in depth
        raise ToolError(f"permission denied: {reason}")
    tool_def = ToolDefinition.objects.get(slug=slug)
    _validate(tool_def.input_schema, data)
    started = time.monotonic()
    try:
        result = HANDLERS[slug](ctx, **data)
        ok = True
    except ToolError as exc:
        result, ok = {"error": str(exc)}, False
    except Exception as exc:
        result, ok = {"error": f"tool failed: {exc.__class__.__name__}"}, False
    return ok, result, int((time.monotonic() - started) * 1000)


def summarize(result, limit=4000):
    import json

    return redact_text(json.dumps(result, default=str)[:limit])
