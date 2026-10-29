from .models import CompanySettings
from .permissions import can


def company(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    user = request.user
    perms = {p: can(user, p) for p in [
        "view_company", "view_employees", "view_tasks", "view_monitor", "view_models", "view_audit",
        "manage_settings", "manage_models", "manage_tools", "manage_departments", "manage_employees",
        "manage_tasks", "review_tasks", "emergency_stop",
    ]}
    return {
        "company": CompanySettings.get(),
        "perm": perms,
        "unread_notifications": user.notifications.filter(is_read=False).count(),
    }
