import hashlib
from datetime import date

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.core import audit
from apps.core.forms import StyledMixin
from apps.core.models import Department, EmployeeProfile
from apps.core.permissions import can, has_company_scope, scope_queryset
from apps.work.models import Project

from . import compute, export
from .models import ReportJob


class ReportForm(StyledMixin, forms.Form):
    kind = forms.ChoiceField(choices=ReportJob.KINDS)
    start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    department = forms.ModelChoiceField(queryset=Department.objects.none(), required=False)
    project = forms.ModelChoiceField(queryset=Project.objects.none(), required=False)
    employee = forms.ModelChoiceField(queryset=EmployeeProfile.objects.none(), required=False)

    def __init__(self, *a, user=None, **kw):
        super().__init__(*a, **kw)
        self.fields["department"].queryset = scope_queryset(user, "view_reports", Department.objects.all(), dept_field="pk", project_field=None)
        self.fields["project"].queryset = scope_queryset(user, "view_reports", Project.objects.all(), project_field="pk")
        self.fields["employee"].queryset = scope_queryset(user, "view_reports", EmployeeProfile.objects.all(), project_field=None)

    def clean(self):
        d = super().clean()
        if d.get("start") and d.get("end") and d["start"] > d["end"]:
            raise forms.ValidationError("Start date must be before end date.")
        if d.get("kind") == "department" and not d.get("department"):
            self.add_error("department", "Choose a department.")
        if d.get("kind") == "employee" and not d.get("employee"):
            self.add_error("employee", "Choose an employee.")
        return d


def authorize(user, kind, department=None, employee=None, project=None):
    """Server-side report access check (also re-run on download)."""
    if kind in ("company", "financial"):
        ok = has_company_scope(user, "view_reports")
    elif kind == "department":
        ok = department is not None and can(user, "view_reports", department)
    elif kind == "employee":
        ok = employee is not None and can(user, "view_reports", employee)
    else:
        ok = False
    if ok and project is not None and not can(user, "view_reports", project):
        ok = False
    if not ok:
        raise PermissionDenied("Not authorized for this report")


def build(kind, d):
    f = compute.Filters(d.get("start"), d.get("end"), d.get("department"), d.get("project"), d.get("employee"))
    if kind == "company":
        return compute.company_report(f)
    if kind == "financial":
        return compute.financial_report(f)
    if kind == "department":
        return compute.department_report(d["department"], f)
    return compute.employee_report(d["employee"], f)


def _params(d):
    return {"start": str(d["start"]) if d.get("start") else None, "end": str(d["end"]) if d.get("end") else None,
            "department": d["department"].pk if d.get("department") else None, "project": d["project"].pk if d.get("project") else None,
            "employee": d["employee"].pk if d.get("employee") else None}


def _from_params(p):
    return {"start": date.fromisoformat(p["start"]) if p.get("start") else None, "end": date.fromisoformat(p["end"]) if p.get("end") else None,
            "department": Department.objects.filter(pk=p.get("department")).first() if p.get("department") else None,
            "project": Project.objects.filter(pk=p.get("project")).first() if p.get("project") else None,
            "employee": EmployeeProfile.objects.filter(pk=p.get("employee")).first() if p.get("employee") else None}


def reports_home(request):
    if not can(request.user, "view_reports"):
        raise PermissionDenied
    data = request.POST if request.method == "POST" else (request.GET if "kind" in request.GET else None)
    form = ReportForm(data, user=request.user)
    report = None
    if data is not None and form.is_valid():
        d = form.cleaned_data
        authorize(request.user, d["kind"], d.get("department"), d.get("employee"), d.get("project"))
        if request.method == "POST":
            fmt = request.POST.get("fmt")
            if fmt not in ("csv", "pdf"):
                raise Http404
            job = ReportJob.objects.create(kind=d["kind"], fmt=fmt, params=_params(d), requested_by=request.user)
            try:
                rep = build(d["kind"], d)
                content = export.to_csv(rep) if fmt == "csv" else export.to_pdf(rep)
                root = settings.VCOOS_REPORT_ROOT
                root.mkdir(parents=True, exist_ok=True)
                job.file_name = f"report-{job.pk}-{d['kind']}.{fmt}"
                (root / job.file_name).write_bytes(content)
                job.sha256, job.status, job.finished_at = hashlib.sha256(content).hexdigest(), "done", timezone.now()
            except Exception as exc:
                job.status, job.error = "failed", f"{exc.__class__.__name__}: {exc}"[:2000]
            job.save()
            audit.record("report.exported", request=request, target=job, details={"kind": job.kind, "fmt": fmt, "status": job.status})
            if job.status == "done":
                return redirect("report_download", job.pk)
            messages.error(request, f"Export failed: {job.error}")
        else:
            report = build(d["kind"], d)
    jobs = request.user.report_jobs.all()[:15]
    return render(request, "reports/home.html", {"form": form, "report": report, "jobs": jobs, "query": request.GET.urlencode()})


def report_download(request, pk):
    job = get_object_or_404(ReportJob, pk=pk, status="done")
    if job.requested_by_id != request.user.pk and not request.user.is_founder:
        raise PermissionDenied
    p = _from_params(job.params)
    authorize(request.user, job.kind, p["department"], p["employee"], p["project"])  # access may have been revoked since
    path = settings.VCOOS_REPORT_ROOT / job.file_name
    if not path.is_file():
        raise Http404("Report file no longer available")
    ctype = "text/csv" if job.fmt == "csv" else "application/pdf"
    return FileResponse(open(path, "rb"), as_attachment=True, filename=job.file_name, content_type=ctype)
