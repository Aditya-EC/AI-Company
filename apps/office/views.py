from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from apps.core.models import EmployeeProfile
from apps.core.permissions import require
from apps.gateway.models import ModelProvider, UsageRecord

from .state import OPEN_STATUSES, office_state, serialize_employee


def office(request):
    require(request.user, "view_employees")
    return render(request, "office/office.html", {"force_2d": request.GET.get("mode") == "2d"})


def monitor(request):
    require(request.user, "view_monitor")
    return render(request, "office/monitor.html", {
        "providers": ModelProvider.objects.all(),
        "recent_usage": UsageRecord.objects.select_related("model", "employee", "task")[:25],
    })


@require_GET
def api_office_state(request):
    require(request.user, "view_employees")
    return JsonResponse(office_state(request.user))


@require_GET
def api_employee(request, pk):
    emp = get_object_or_404(EmployeeProfile.objects.select_related("department"), pk=pk)
    require(request.user, "view_employees", emp)
    open_tasks = list(emp.tasks.filter(status__in=OPEN_STATUSES).select_related("project").order_by("priority", "-updated_at"))
    return JsonResponse(serialize_employee(emp, open_tasks, detail=True))
