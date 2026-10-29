from django import forms
from django.contrib.auth.password_validation import validate_password
from django.utils.text import slugify

from apps.gateway.models import AIModel, ModelProvider
from apps.work.models import Task

from .models import Budget, CompanySettings, Department, EmployeeProfile, PermissionAssignment, RoleTemplate, User


class StyledMixin:
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        for f in self.fields.values():
            if isinstance(f.widget, forms.CheckboxInput):
                f.widget.attrs.setdefault("class", "check")
            else:
                f.widget.attrs.setdefault("class", "input")


def _lines_to_list(text):
    return [s.strip() for s in (text or "").replace(",", "\n").splitlines() if s.strip()]


class DepartmentForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Department
        fields = ["name", "description", "instructions", "manager", "color", "office_x", "office_z", "office_floor", "is_active"]
        widgets = {"color": forms.TextInput(attrs={"type": "color"}), "description": forms.Textarea(attrs={"rows": 2}),
                   "instructions": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        qs = EmployeeProfile.objects.all()
        if self.instance.pk:
            qs = qs.filter(department=self.instance)
        else:
            qs = qs.none()
        self.fields["manager"].queryset = qs
        self.fields["manager"].required = False

    def save(self, commit=True):
        obj = super().save(commit=False)
        if not obj.slug:
            base, i = slugify(obj.name)[:100] or "dept", 1
            obj.slug = base
            while Department.objects.filter(slug=obj.slug).exclude(pk=obj.pk).exists():
                i += 1
                obj.slug = f"{base}-{i}"
        if commit:
            obj.save()
        return obj


class EmployeeForm(StyledMixin, forms.ModelForm):
    skills_text = forms.CharField(label="Skills", required=False, help_text="Comma or line separated.",
                                  widget=forms.Textarea(attrs={"rows": 2}))

    class Meta:
        model = EmployeeProfile
        fields = ["name", "job_title", "department", "role_template", "manager", "role_description", "system_instructions",
                  "avatar_color", "max_concurrent_tasks", "budget_limit_usd"]
        widgets = {"avatar_color": forms.TextInput(attrs={"type": "color"}), "role_description": forms.Textarea(attrs={"rows": 2}),
                   "system_instructions": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["department"].queryset = Department.objects.filter(is_active=True)
        self.fields["role_template"].required = False
        self.fields["manager"].required = False
        if self.instance.pk:
            self.fields["skills_text"].initial = ", ".join(self.instance.skills)
            self.fields["manager"].queryset = EmployeeProfile.objects.exclude(pk=self.instance.pk)
        self.fields["job_title"].required = False

    def clean(self):
        data = super().clean()
        tpl = data.get("role_template")
        if tpl and not data.get("job_title"):
            data["job_title"] = tpl.job_title
        if not data.get("job_title"):
            self.add_error("job_title", "Job title is required (or choose a role template).")
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        tpl = self.cleaned_data.get("role_template")
        skills = _lines_to_list(self.cleaned_data.get("skills_text"))
        if tpl and not obj.pk:
            obj.role_description = obj.role_description or tpl.description
            obj.system_instructions = obj.system_instructions or tpl.system_instructions
            skills = skills or list(tpl.skills)
        obj.skills = skills
        obj.job_title = self.cleaned_data["job_title"]
        if commit:
            obj.save()
        return obj


class RoleTemplateForm(StyledMixin, forms.ModelForm):
    skills_text = forms.CharField(label="Skills", required=False)

    class Meta:
        model = RoleTemplate
        fields = ["name", "category", "job_title", "description", "system_instructions"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2}), "system_instructions": forms.Textarea(attrs={"rows": 3})}

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.skills = _lines_to_list(self.cleaned_data.get("skills_text"))
        if commit:
            obj.save()
        return obj


class ProviderForm(StyledMixin, forms.ModelForm):
    base_url = forms.URLField(max_length=300, assume_scheme="http", help_text="Local Ollama is usually http://localhost:11434")
    api_key = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False), label="API key (stored encrypted)",
                              help_text="Leave blank to keep the current key. Prefer a credential reference like env:OPENAI_API_KEY.")

    class Meta:
        model = ModelProvider
        fields = ["name", "kind", "base_url", "credential_ref", "is_enabled", "is_local", "is_paid", "allow_confidential_data",
                  "privacy_notes", "rate_limit_rpm", "timeout_seconds"]
        widgets = {"privacy_notes": forms.Textarea(attrs={"rows": 2})}

    def clean_credential_ref(self):
        ref = self.cleaned_data.get("credential_ref", "").strip()
        if ref and not ref.startswith("env:"):
            raise forms.ValidationError("Use the form env:VARIABLE_NAME. Paste raw keys into the API key field instead.")
        return ref

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("api_key"):
            obj.set_api_key(self.cleaned_data["api_key"])
        if commit:
            obj.save()
        return obj


class AIModelForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = AIModel
        fields = ["provider", "model_identifier", "display_name", "context_window", "max_output_tokens", "supports_json",
                  "supports_tools", "supports_streaming", "supports_vision", "price_input_per_mtok", "price_output_per_mtok",
                  "daily_request_quota", "is_enabled", "is_approved", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class TaskForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "description", "acceptance_criteria", "department", "assigned_employee", "project", "priority",
                  "requires_review", "reviewer", "requires_approval", "is_confidential", "is_idempotent", "max_steps",
                  "max_retries", "max_runtime_seconds", "max_cost_usd", "deadline"]
        widgets = {"description": forms.Textarea(attrs={"rows": 5}), "acceptance_criteria": forms.Textarea(attrs={"rows": 3}),
                   "deadline": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    depends_on = forms.ModelMultipleChoiceField(queryset=Task.objects.none(), required=False,
                                                help_text="Task will stay blocked until these complete.")
    skills_text = forms.CharField(label="Required skills", required=False,
                                  help_text="Comma separated; used when the engine picks the assignee.")

    def __init__(self, *a, user=None, **kw):
        super().__init__(*a, **kw)
        from .permissions import scope_queryset

        self.fields["department"].queryset = scope_queryset(user, "manage_tasks", Department.objects.filter(is_active=True), dept_field="pk", project_field=None)
        self.fields["assigned_employee"].queryset = scope_queryset(user, "manage_tasks", EmployeeProfile.objects.filter(status="active"), project_field=None)
        self.fields["assigned_employee"].required = False
        self.fields["assigned_employee"].help_text = "Leave empty to let the engine assign by skills and workload."
        self.fields["reviewer"].queryset = User.objects.filter(is_active=True)
        self.fields["reviewer"].required = False
        self.fields["project"].required = False
        self.fields["depends_on"].queryset = scope_queryset(user, "view_tasks", Task.objects.exclude(status__in=["cancelled"]))
        self.fields["depends_on"].widget.attrs["class"] = "input"

    def clean(self):
        data = super().clean()
        emp, dept = data.get("assigned_employee"), data.get("department")
        if emp and dept and emp.department_id != dept.pk:
            self.add_error("assigned_employee", "Employee belongs to a different department.")
        return data


class _Lines(forms.CharField):
    def __init__(self, **kw):
        super().__init__(required=False, widget=forms.Textarea(attrs={"rows": 2}), help_text="One per line.", **kw)

    def prepare_value(self, value):
        return "\n".join(value) if isinstance(value, list) else value

    def to_python(self, value):
        return _lines_to_list(value)


class CompanySettingsForm(StyledMixin, forms.ModelForm):
    permitted_software_categories = _Lines()
    permitted_languages = _Lines()
    permitted_frameworks = _Lines()
    web_allowed_domains = _Lines()

    class Meta:
        model = CompanySettings
        fields = ["name", "mission", "strategy", "development_standards", "max_concurrent_tasks", "default_max_retries",
                  "default_max_runtime_seconds", "default_max_steps", "ceo_user", "ceo_employee", "ceo_approval_limit_usd",
                  "permitted_software_categories", "permitted_languages", "permitted_frameworks", "licensing_policy",
                  "testing_rules", "security_requirements", "deployment_restrictions", "repository_conventions",
                  "web_allowed_domains"]
        widgets = {f: forms.Textarea(attrs={"rows": 3}) for f in
                   ["mission", "strategy", "development_standards", "licensing_policy", "testing_rules", "security_requirements",
                    "deployment_restrictions", "repository_conventions"]}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["ceo_user"].queryset = User.objects.filter(account_type=User.CEO, is_active=True)
        self.fields["ceo_user"].required = False
        self.fields["ceo_employee"].required = False


class BudgetForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Budget
        fields = ["scope", "period", "limit_usd", "department", "employee", "project", "provider", "is_active"]


class UserCreateForm(StyledMixin, forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ["username", "email", "first_name", "last_name", "account_type", "department"]

    def clean_account_type(self):
        t = self.cleaned_data["account_type"]
        if t == User.FOUNDER:
            raise forms.ValidationError("There is only one Founder; create CEO or Staff accounts here.")
        return t

    def clean_password(self):
        pw = self.cleaned_data["password"]
        validate_password(pw)
        return pw

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user


class PermissionAssignmentForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = PermissionAssignment
        fields = ["user", "permission", "department", "project"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["user"].queryset = User.objects.filter(account_type=User.STAFF, is_active=True)
        self.fields["department"].required = False
        self.fields["project"].required = False

