from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from apps.core import audit
from apps.core.models import Department
from apps.core.permissions import can, require

from .models import ToolDefinition, ToolPermission
from .registry import sync_definitions


def tool_registry(request):
    require(request.user, "view_models")
    manage = can(request.user, "manage_tools")
    if request.method == "POST":
        require(request.user, "manage_tools")
        action = request.POST.get("action")
        if action == "sync":
            sync_definitions()
            audit.record("tool.registry_synced", request=request)
        elif action == "toggle":
            t = get_object_or_404(ToolDefinition, pk=request.POST.get("tool"))
            t.is_enabled = not t.is_enabled
            t.save(update_fields=["is_enabled"])
            audit.record("tool.toggled", target=t, request=request, details={"enabled": t.is_enabled})
        elif action == "grant":
            t = get_object_or_404(ToolDefinition, pk=request.POST.get("tool"))
            scope = request.POST.get("scope")
            kw = {"tool": t, "scope": scope, "allow": request.POST.get("effect") != "deny", "granted_by": request.user}
            if scope == "department":
                kw["department"] = get_object_or_404(Department, pk=request.POST.get("department"))
            elif scope != "company":
                messages.error(request, "Use the employee page for employee-level grants.")
                return redirect("tool_registry")
            ToolPermission.objects.filter(tool=t, scope=scope, department=kw.get("department")).delete()
            ToolPermission.objects.create(**kw)
            audit.record("tool.permission_set", target=t, request=request,
                         details={"scope": scope, "department": request.POST.get("department"), "allow": kw["allow"]})
        elif action == "revoke":
            g = get_object_or_404(ToolPermission, pk=request.POST.get("grant"))
            audit.record("tool.permission_revoked", target=g.tool, request=request, details={"scope": g.scope})
            g.delete()
        messages.success(request, "Tool registry updated.")
        return redirect("tool_registry")
    return render(request, "tools/registry.html", {
        "tools": ToolDefinition.objects.prefetch_related("permissions__department", "permissions__employee"),
        "departments": Department.objects.filter(is_active=True), "manage": manage,
    })
