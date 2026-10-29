"""Knowledge base: permission-aware access, search, and controlled (reviewed) updates.

Rules:
- Readers and agents only ever receive a document's *approved* current version.
- Anything written by an AI employee or taken from external content starts as a pending version and
  never becomes authoritative without a human decision.
- Policies, coding standards and procedures (authoritative categories) can only be approved by the Founder.
- Nobody except the Founder approves their own version.
"""
import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core import audit
from apps.core.notify import notify_leadership
from apps.core.permissions import can

from .models import KnowledgeDocument as Doc
from .models import KnowledgeVersion as Version

WORD = re.compile(r"[a-z0-9][a-z0-9_\-]{2,}")
STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "will", "must", "should", "into", "task", "using"}


class KnowledgeError(Exception):
    pass


# ------------------------------------------------------------------ access

def _grants(user, perm):
    return list(user.permission_assignments.filter(permission=perm).values("department_id", "project_id"))


def visible_documents(user):
    """Documents a human user may read (approved content) or manage."""
    qs = Doc.objects.exclude(status=Doc.ARCHIVED)
    if not user.is_authenticated or not user.is_active:
        return qs.none()
    if user.is_founder or user.is_ceo:
        return qs
    grants = _grants(user, "view_knowledge") + _grants(user, "manage_knowledge")
    if not grants:
        return qs.none()
    company_wide = any(g["department_id"] is None and g["project_id"] is None for g in grants)
    cond = Q(access=Doc.COMPANY)
    if company_wide:
        cond |= Q(access__in=[Doc.DEPARTMENT, Doc.PROJECT])
    else:
        dept_ids = [g["department_id"] for g in grants if g["department_id"]]
        proj_ids = [g["project_id"] for g in grants if g["project_id"]]
        cond |= Q(access=Doc.DEPARTMENT, department_id__in=dept_ids) | Q(access=Doc.PROJECT, project_id__in=proj_ids)
    return qs.filter(cond)


def can_read(user, doc):
    return visible_documents(user).filter(pk=doc.pk).exists()


def can_write(user, doc=None, *, department=None, project=None):
    if user.is_founder or user.is_ceo:
        return True
    target = doc or project or department
    return can(user, "manage_knowledge", target) if target is not None else can(user, "manage_knowledge")


def can_approve(user, version):
    doc = version.document
    if doc.category in Doc.AUTHORITATIVE:
        return user.is_founder
    if version.author_user_id == user.pk and not user.is_founder:
        return False
    if user.is_founder or user.is_ceo:
        return True
    target = doc.project or doc.department
    return can(user, "manage_knowledge", target) if target else False


def agent_documents(employee, task=None):
    """Approved documents an AI employee may receive: company-wide, its department, its task's project."""
    cond = Q(access=Doc.COMPANY) | Q(access=Doc.DEPARTMENT, department_id=employee.department_id)
    if task is not None and task.project_id:
        cond |= Q(access=Doc.PROJECT, project_id=task.project_id)
    return Doc.objects.filter(cond, status=Doc.APPROVED, current_version__isnull=False).select_related("current_version")


# ------------------------------------------------------------------ search

def terms(text):
    return [w for w in WORD.findall((text or "").lower()) if w not in STOP][:40]


def search(qs, query, limit=10):
    """Keyword relevance over title (x3), tags (x2) and approved content. Returns [(doc, score, snippet)]."""
    words = list(dict.fromkeys(terms(query)))
    if not words:
        return []
    cond = Q()
    for w in words:
        cond |= Q(title__icontains=w) | Q(current_version__content__icontains=w)
    results = []
    for doc in qs.filter(cond).select_related("current_version").distinct()[:200]:
        content = doc.current_version.content if doc.current_version else ""
        title, low = doc.title.lower(), content.lower()
        tags = " ".join(doc.tags).lower()
        score = sum(3 * title.count(w) + 2 * tags.count(w) + min(low.count(w), 10) for w in words)
        if score <= 0:
            continue
        pos = min((low.find(w) for w in words if low.find(w) >= 0), default=0)
        start = max(pos - 120, 0)
        results.append((doc, score, ("…" if start else "") + content[start:start + 400]))
    results.sort(key=lambda r: (-r[1], r[0].title))
    return results[:limit]


def context_for_task(employee, task, limit=3, chars=1200):
    """Relevant approved knowledge for a task prompt (scoped to what the employee may see)."""
    hits = search(agent_documents(employee, task), f"{task.title} {task.description[:1500]}", limit=limit)
    return [{"id": d.pk, "title": d.title, "category": d.get_category_display(), "version": d.current_version.number,
             "content": d.current_version.content[:chars]} for d, score, _ in hits if score >= 2]


# ------------------------------------------------------------------ writes

def create_document(*, title, category, content, access=Doc.COMPANY, department=None, project=None, user=None, employee=None,
                    source=None, source_url="", source_task=None, tags=None, change_note="initial version"):
    if category not in dict(Doc.CATEGORIES) or access not in dict(Doc.ACCESS):
        raise KnowledgeError("Unknown category or access level")
    if access == Doc.DEPARTMENT and department is None:
        raise KnowledgeError("Department documents need a department")
    if access == Doc.PROJECT and project is None:
        raise KnowledgeError("Project documents need a project")
    if not title.strip() or not content.strip():
        raise KnowledgeError("Title and content are required")
    source = source or ("agent" if employee else "human")
    if source_url and source == "human":
        source = "external"
    with transaction.atomic():
        doc = Doc.objects.create(title=title.strip()[:200], category=category, access=access, department=department, project=project,
                                 source=source, source_url=source_url[:500], source_task=source_task, tags=tags or [],
                                 created_by=user, status=Doc.IN_REVIEW)
        version = Version.objects.create(document=doc, number=1, content=content, change_note=change_note[:300],
                                         author_user=user, author_employee=employee)
        audit.record("knowledge.created", user=user, employee=employee, target=doc,
                     details={"category": category, "access": access, "source": source})
    notify_leadership("knowledge", f"Knowledge review needed: {doc.title}", f"{doc.get_category_display()} v1", f"/knowledge/{doc.pk}/")
    return doc, version


def propose_version(doc, *, content, change_note="", user=None, employee=None):
    if doc.status == Doc.ARCHIVED:
        raise KnowledgeError("Archived documents cannot be changed")
    if not content.strip():
        raise KnowledgeError("Content is required")
    with transaction.atomic():
        number = (doc.versions.order_by("-number").values_list("number", flat=True).first() or 0) + 1
        version = Version.objects.create(document=doc, number=number, content=content, change_note=change_note[:300],
                                         author_user=user, author_employee=employee)
        if doc.current_version_id is None:
            doc.status = Doc.IN_REVIEW
            doc.save(update_fields=["status", "updated_at"])
        audit.record("knowledge.version_proposed", user=user, employee=employee, target=doc, details={"version": number})
    notify_leadership("knowledge", f"Knowledge review needed: {doc.title} v{number}", change_note[:200], f"/knowledge/{doc.pk}/")
    return version


def review_version(version, *, user, approve, note=""):
    if version.status != Version.PENDING:
        raise KnowledgeError("This version was already reviewed")
    if not can_approve(user, version):
        doc = version.document
        if doc.category in Doc.AUTHORITATIVE:
            raise KnowledgeError("Only the Founder can approve policies, standards and procedures")
        raise KnowledgeError("You are not authorized to approve this version (authors cannot approve their own changes)")
    doc = version.document
    with transaction.atomic():
        version.status = Version.APPROVED if approve else Version.REJECTED
        version.reviewed_by, version.reviewed_at, version.review_note = user, timezone.now(), note[:500]
        version.save()
        if approve:
            if doc.current_version_id:
                Version.objects.filter(pk=doc.current_version_id).update(status=Version.SUPERSEDED)
            doc.current_version, doc.status = version, Doc.APPROVED
        elif doc.current_version_id is None and not doc.versions.filter(status=Version.PENDING).exists():
            doc.status = Doc.DRAFT
        doc.save()
        audit.record("knowledge.reviewed", user=user, target=doc, details={"version": version.number, "approved": approve,
                                                                           "authoritative": doc.category in Doc.AUTHORITATIVE})
    return version


def archive(doc, *, user):
    if doc.category in Doc.AUTHORITATIVE and not user.is_founder:
        raise KnowledgeError("Only the Founder can archive policies, standards and procedures")
    doc.status = Doc.ARCHIVED
    doc.save(update_fields=["status", "updated_at"])
    audit.record("knowledge.archived", user=user, target=doc)


def record_system_document(*, title, category, content, access, department=None, project=None, source_task=None):
    """Factual system records (e.g. meeting notes) are stored as approved, non-authoritative documents."""
    if category in Doc.AUTHORITATIVE:
        raise KnowledgeError("System records cannot be authoritative")
    with transaction.atomic():
        doc = Doc.objects.create(title=title[:200], category=category, access=access, department=department, project=project,
                                 source="system", source_task=source_task, status=Doc.APPROVED)
        v = Version.objects.create(document=doc, number=1, content=content, change_note="system record", status=Version.APPROVED,
                                   reviewed_at=timezone.now(), review_note="system record (not authoritative)")
        doc.current_version = v
        doc.save(update_fields=["current_version"])
        audit.record("knowledge.system_record", target=doc, details={"category": category})
    return doc

