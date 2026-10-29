"""In-app notifications fan-out."""
from .models import Notification, User


def notify_leadership(kind, title, body="", link=""):
    users = User.objects.filter(is_active=True, account_type__in=[User.FOUNDER, User.CEO])
    Notification.objects.bulk_create([Notification(user=u, kind=kind, title=title, body=body, link=link) for u in users])


def notify_user(user, kind, title, body="", link=""):
    if user is not None:
        Notification.objects.create(user=user, kind=kind, title=title, body=body, link=link)
