from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.forms import StyledMixin, _lines_to_list
from apps.core.models import Department, EmployeeProfile
from apps.core.permissions import can
from apps.work.models import Project

from . import meetings as msvc
from . import services as kb
from .models import KnowledgeDocument as Doc
from .models import KnowledgeVersion, Meeting


def _can_view(user):
    return user.is_founder or user.is_ceo or can(user, "view_knowledge") or can(user, "manage_knowledge")


class DocumentForm(StyledMixin, forms.Form):
    title = forms.CharField(max_length=200)
    category = forms.ChoiceField(choices=[c for c in Doc.CATEGORIES if c[0] != "meeting_notes"])
    access = forms.ChoiceField(choices=Doc.ACCESS)
    department = forms.ModelChoiceField(queryset=Department.objects.filter(is_active=True), required=False)
    project = forms.ModelChoiceField(queryset=Project.objects.all(), required=False)
    tags = forms.CharField(required=False, help_text="Comma separated.")
    source_url = forms.URLField(required=False, assume_scheme="https", help_text="If the content comes from an external source.")
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 14}))


class VersionForm(StyledMixin, forms.Form):
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 14}))
    change_note = forms.CharField(max_length=300)


def knowledge_list(request):
    if not _can_view(request.user):
        raise PermissionDenied
    qs = kb.visible_documents(request.user).select_related("current_version", "department", "project")
    q, cat = request.GET.get("q", "").strip(), request.GET.get("category", "")
    if cat:
        qs = qs.filter(category=cat)
    if q:
        results = [(d, snip) for d, _, snip in kb.search(qs.filter(current_version__isnull=False), q, limit=50)]
    else:
        results = [(d, "") for d in qs.order_by("category", "title")]
    page = Paginator(results, 30).get_page(request.GET.get("page"))
    pending = KnowledgeVersion.objects.filter(status=KnowledgeVersion.PENDING, document__in=qs).select_related("document")[:20]
    return render(request, "knowledge/list.html", {"page": page, "q": q, "category": cat, "categories": Doc.CATEGORIES,
                                                   "pending": pending, "can_write": kb.can_write(request.user)})


def knowledge_create(request):
    if not kb.can_write(request.user):
        raise PermissionDenied
    form = DocumentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if not kb.can_write(request.user, department=d["department"], project=d["project"]):
            raise PermissionDenied
        try:
            doc, _ = kb.create_document(title=d["title"], category=d["category"], access=d["access"], department=d["department"],
                                        project=d["project"], content=d["content"], user=request.user, source_url=d["source_url"],
                                        tags=_lines_to_list(d["tags"]))
            messages.success(request, "Document created as version 1, pending review.")
            return redirect("knowledge_detail", doc.pk)
        except kb.KnowledgeError as exc:
            messages.error(request, str(exc))
    return render(request, "knowledge/form.html", {"form": form})


def knowledge_detail(request, pk):
    doc = get_object_or_404(Doc.objects.select_related("current_version", "department", "project"), pk=pk)
    if not kb.can_read(request.user, doc):
        raise PermissionDenied
    writable = kb.can_write(request.user, doc)
    vform = VersionForm(request.POST if request.POST.get("action") == "propose" else None) if writable else None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "propose":
                if not writable:
                    raise PermissionDenied
                if vform.is_valid():
                    v = kb.propose_version(doc, user=request.user, **vform.cleaned_data)
                    messages.success(request, f"Version {v.number} submitted for review.")
                    return redirect("knowledge_detail", pk)
            elif action in ("approve", "reject"):
                v = get_object_or_404(KnowledgeVersion, pk=request.POST.get("version"), document=doc)
                kb.review_version(v, user=request.user, approve=action == "approve", note=request.POST.get("note", ""))
                messages.success(request, f"Version {v.number} {'approved' if action == 'approve' else 'rejected'}.")
                return redirect("knowledge_detail", pk)
            elif action == "archive":
                if not writable:
                    raise PermissionDenied
                kb.archive(doc, user=request.user)
                messages.success(request, "Document archived.")
                return redirect("knowledge_list")
        except kb.KnowledgeError as exc:
            messages.error(request, str(exc))
    versions = doc.versions.select_related("author_user", "author_employee", "reviewed_by")
    return render(request, "knowledge/detail.html", {
        "doc": doc, "versions": versions, "vform": vform, "writable": writable,
        "reviewable": {v.pk for v in versions if v.status == KnowledgeVersion.PENDING and kb.can_approve(request.user, v)},
    })


# ------------------------------------------------------------------ meetings

class MeetingForm(StyledMixin, forms.Form):
    title = forms.CharField(max_length=200)
    kind = forms.ChoiceField(choices=Meeting.KINDS)
    purpose = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), help_text="The decision or output this meeting must produce.")
    agenda = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), help_text="One item per line.")
    participants = forms.ModelMultipleChoiceField(queryset=EmployeeProfile.objects.filter(status="active"),
                                                  help_text=f"2-{msvc.MAX_PARTICIPANTS} AI employees.")
    facilitator = forms.ModelChoiceField(queryset=EmployeeProfile.objects.filter(status="active"))
    project = forms.ModelChoiceField(queryset=Project.objects.all(), required=False)
    department = forms.ModelChoiceField(queryset=Department.objects.filter(is_active=True), required=False)
    max_rounds = forms.IntegerField(min_value=1, max_value=msvc.MAX_ROUNDS, initial=2)


def meeting_list(request):
    if not can(request.user, "view_tasks"):
        raise PermissionDenied
    manage = can(request.user, "manage_tasks")
    form = MeetingForm(request.POST or None) if manage else None
    if request.method == "POST":
        if not manage:
            raise PermissionDenied
        if form.is_valid():
            d = form.cleaned_data
            try:
                m = msvc.create_meeting(user=request.user, title=d["title"], kind=d["kind"], purpose=d["purpose"],
                                        agenda=[x.strip() for x in d["agenda"].splitlines() if x.strip()],
                                        participants=d["participants"], facilitator=d["facilitator"], project=d["project"],
                                        department=d["department"], max_rounds=d["max_rounds"])
                if request.POST.get("start") == "1":
                    msvc.queue_meeting(m, user=request.user)
                messages.success(request, f"{m.code} created" + (" and queued" if m.status == Meeting.QUEUED else ""))
                return redirect("meeting_detail", m.pk)
            except msvc.MeetingError as exc:
                messages.error(request, str(exc))
    meetings = Meeting.objects.select_related("facilitator", "project")
    if not (request.user.is_founder or request.user.is_ceo):
        from apps.core.permissions import scope_queryset

        meetings = scope_queryset(request.user, "view_tasks", meetings, project_field="project")
    return render(request, "meetings/list.html", {"meetings": meetings[:50], "form": form})


def _can_see_meeting(user, m):
    if user.is_founder or user.is_ceo:
        return True
    target = m.project or m.department
    return target is not None and can(user, "view_tasks", target)


def meeting_detail(request, pk):
    m = get_object_or_404(Meeting.objects.select_related("facilitator", "project", "department", "notes_document"), pk=pk)
    if not _can_see_meeting(request.user, m):
        raise PermissionDenied
    return render(request, "meetings/detail.html", {
        "m": m, "turns": m.turns.filter(attempt=m.attempt).select_related("employee", "model"),
        "participants": m.participants.all(), "tasks": m.action_tasks.all(), "manage": can(request.user, "manage_tasks"),
    })


@require_POST
def meeting_action(request, pk):
    m = get_object_or_404(Meeting, pk=pk)
    if not can(request.user, "manage_tasks") or not _can_see_meeting(request.user, m):
        raise PermissionDenied
    action = request.POST.get("action")
    try:
        if action == "queue":
            msvc.queue_meeting(m, user=request.user)
            messages.success(request, "Meeting queued; the engine will run it.")
        elif action == "cancel":
            msvc.cancel_meeting(m, user=request.user)
            messages.success(request, "Meeting cancelled.")
        elif action == "queue_actions":
            from apps.work import services

            n = 0
            for t in m.action_tasks.filter(status="draft"):
                services.queue_task(t, user=request.user, reason=f"action item from {m.code}")
                n += 1
            messages.success(request, f"{n} action items submitted to the engine.")
    except msvc.MeetingError as exc:
        messages.error(request, str(exc))
    return redirect("meeting_detail", pk)
