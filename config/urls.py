from django.contrib.auth import views as auth_views
from django.urls import path

from apps.core import views as core
from apps.gateway import views as gateway
from apps.office import views as office
from apps.tools import views as tools
from apps.work import factory_views as factory
from apps.work import views as work

auth = [
    path("accounts/login/", core.RateLimitedLoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/password-reset/", auth_views.PasswordResetView.as_view(template_name="auth/password_reset.html",
         email_template_name="auth/password_reset_email.txt"), name="password_reset"),
    path("accounts/password-reset/done/", auth_views.PasswordResetDoneView.as_view(template_name="auth/password_reset_done.html"),
         name="password_reset_done"),
    path("accounts/reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(template_name="auth/password_reset_confirm.html"),
         name="password_reset_confirm"),
    path("accounts/reset/done/", auth_views.PasswordResetCompleteView.as_view(template_name="auth/password_reset_complete.html"),
         name="password_reset_complete"),
    path("accounts/password/", auth_views.PasswordChangeView.as_view(template_name="auth/password_change.html", success_url="/"),
         name="password_change"),
]

urlpatterns = auth + [
    path("", core.dashboard, name="dashboard"),
    path("healthz", core.healthz, name="healthz"),
    path("office/", office.office, name="office"),
    path("monitor/", office.monitor, name="monitor"),
    path("api/office/state/", office.api_office_state, name="api_office_state"),
    path("api/employees/<int:pk>/", office.api_employee, name="api_employee"),
    path("departments/", core.department_list, name="department_list"),
    path("departments/<int:pk>/", core.department_detail, name="department_detail"),
    path("employees/", core.employee_list, name="employee_list"),
    path("employees/new/", core.employee_create, name="employee_create"),
    path("employees/templates/", core.role_templates, name="role_templates"),
    path("employees/<int:pk>/", core.employee_detail, name="employee_detail"),
    path("models/", gateway.provider_list, name="provider_list"),
    path("models/providers/<int:pk>/", gateway.provider_detail, name="provider_detail"),
    path("models/<int:pk>/", gateway.model_detail, name="model_detail"),
    path("tools/", tools.tool_registry, name="tool_registry"),
    path("tasks/", work.task_board, name="task_board"),
    path("tasks/new/", work.task_create, name="task_create"),
    path("tasks/<int:pk>/", work.task_detail, name="task_detail"),
    path("tasks/<int:pk>/action/", work.task_action, name="task_action"),
    path("approvals/", work.approvals, name="approvals"),
    path("proposals/", factory.proposal_board, name="proposal_board"),
    path("proposals/new/", factory.proposal_create, name="proposal_create"),
    path("proposals/<int:pk>/", factory.proposal_detail, name="proposal_detail"),
    path("projects/", factory.project_list, name="project_list"),
    path("projects/templates/", factory.project_templates, name="project_templates"),
    path("projects/<int:pk>/", factory.project_detail, name="project_detail"),
    path("projects/<int:pk>/action/", factory.project_action, name="project_action"),
    path("audit/", core.audit_log, name="audit_log"),
    path("settings/", core.company_settings, name="company_settings"),
    path("settings/users/", core.users_view, name="users"),
    path("budgets/", core.budgets_view, name="budgets"),
    path("notifications/", core.notifications, name="notifications"),
]
