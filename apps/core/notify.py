"""Notification delivery honoring per-user preferences.

In-app delivery can be switched off per kind except for MANDATORY kinds. Email is sent only when
the Founder has enabled the email integration, the user opted in for that kind, and has an address.
"""
import logging

from django.core.mail import send_mail

from .models import CompanySettings, Notification, NotificationPreference, User
from .redaction import redact_text

log = logging.getLogger("vcoos.notify")


def _prefs(user):
    return {p.kind: p for p in user.notification_preferences.all()}


def deliver(user, kind, title, body="", link="", *, _company=None):
    if user is None or not user.is_active:
        return None
    pref = _prefs(user).get(kind)
    in_app = kind in NotificationPreference.MANDATORY or pref is None or pref.in_app
    note = Notification.objects.create(user=user, kind=kind, title=title[:200], body=body, link=link) if in_app else None
    company = _company or CompanySettings.get()
    if pref is not None and pref.email and company.email_notifications_enabled and user.email:
        try:
            send_mail(f"[{company.name}] {title}"[:200], redact_text(f"{body}\n\n{link}".strip()), None, [user.email])
        except Exception as exc:  # email outages must never break task processing
            log.warning("notification email failed for user %s: %s", user.pk, exc.__class__.__name__)
    return note


def notify_leadership(kind, title, body="", link=""):
    company = CompanySettings.get()
    for u in User.objects.filter(is_active=True, account_type__in=[User.FOUNDER, User.CEO]):
        deliver(u, kind, title, body, link, _company=company)


def notify_founder(kind, title, body="", link=""):
    company = CompanySettings.get()
    for u in User.objects.filter(is_active=True, account_type=User.FOUNDER):
        deliver(u, kind, title, body, link, _company=company)


def notify_user(user, kind, title, body="", link=""):
    return deliver(user, kind, title, body, link)
