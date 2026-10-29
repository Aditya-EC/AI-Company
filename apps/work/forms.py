from django import forms

from apps.core.forms import StyledMixin, _lines_to_list
from apps.core.models import Department, EmployeeProfile

from .models import Priority, ProjectProposal, ProjectTemplate


class ListField(forms.CharField):
    """A list of strings edited as one item per line."""

    widget = forms.Textarea(attrs={"rows": 3})

    def prepare_value(self, value):
        return "\n".join(value) if isinstance(value, list) else value

    def to_python(self, value):
        return _lines_to_list(value)


class CommissionResearchForm(StyledMixin, forms.Form):
    brief = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), help_text="The opportunity or question to research.")
    department = forms.ModelChoiceField(queryset=Department.objects.filter(is_active=True))
    employee = forms.ModelChoiceField(queryset=EmployeeProfile.objects.filter(status="active"), required=False,
                                      help_text="Leave empty to let the engine choose a researcher in that department.")
    category = forms.ChoiceField(required=False)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["category"].choices = [("", "Let the researcher choose")] + [(t.category, t.name) for t in ProjectTemplate.objects.all()]

    def clean(self):
        data = super().clean()
        emp, dept = data.get("employee"), data.get("department")
        if emp and dept and emp.department_id != dept.pk:
            self.add_error("employee", "Employee belongs to a different department.")
        return data


class ProposalForm(StyledMixin, forms.ModelForm):
    proposed_features = ListField()
    recommended_stack = ListField(required=False)
    risks = ListField(required=False)
    assumptions = ListField(required=False)
    uncertainties = ListField(required=False)

    class Meta:
        model = ProjectProposal
        fields = ["title", "software_category", "department", "problem_statement", "target_users", "market_research",
                  "proposed_features", "technical_feasibility", "recommended_stack", "estimated_effort_days", "estimated_cost_usd",
                  "risks", "revenue_hypothesis", "validation_plan", "confidence", "assumptions", "uncertainties"]
        widgets = {f: forms.Textarea(attrs={"rows": 3}) for f in
                   ["problem_statement", "target_users", "market_research", "technical_feasibility", "revenue_hypothesis", "validation_plan"]}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["software_category"] = forms.ChoiceField(
            required=False, choices=[("", "—")] + [(t.category, t.name) for t in ProjectTemplate.objects.all()],
            widget=forms.Select(attrs={"class": "input"}))
        self.fields["department"].queryset = Department.objects.filter(is_active=True)

    def clean_confidence(self):
        c = self.cleaned_data.get("confidence")
        if c is not None and c > 100:
            raise forms.ValidationError("Confidence is 0-100.")
        return c


class ProposalDecisionForm(StyledMixin, forms.Form):
    outcome = forms.ChoiceField(choices=[("approve", "Approve"), ("revision", "Request revision"), ("defer", "Defer"),
                                         ("reject", "Reject"), ("escalate", "Escalate to Founder")])
    rationale = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)
    planning_department = forms.ModelChoiceField(queryset=Department.objects.filter(is_active=True), required=False,
                                                 help_text="On approval: department that plans the project.")
    planner = forms.ModelChoiceField(queryset=EmployeeProfile.objects.filter(status="active"), required=False,
                                     help_text="On approval: optional specific planner.")


class ProjectPriorityForm(StyledMixin, forms.Form):
    priority = forms.TypedChoiceField(choices=Priority.choices, coerce=int)
    reason = forms.CharField(max_length=300)


class ReleaseForm(StyledMixin, forms.Form):
    version = forms.CharField(max_length=40)
    release_notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}))
    known_issues = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
    deployment_plan = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class ProjectTemplateForm(StyledMixin, forms.ModelForm):
    default_milestones = ListField(required=False)
    default_stack = ListField(required=False)

    class Meta:
        model = ProjectTemplate
        fields = ["category", "name", "description", "default_milestones", "default_stack", "uses_repository"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}
