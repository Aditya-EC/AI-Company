from django import template

register = template.Library()

BADGES = {
    "completed": "b-ok", "running": "b-info", "queued": "b-info", "assigned": "b-info",
    "under_review": "b-purple", "awaiting_approval": "b-purple", "revision_required": "b-warn",
    "blocked": "b-warn", "waiting_input": "b-warn", "paused": "", "failed": "b-bad", "cancelled": "",
    "healthy": "b-ok", "degraded": "b-warn", "down": "b-bad", "auth_error": "b-bad", "unknown": "",
    "active": "b-ok", "suspended": "b-bad", "offline": "", "pending": "b-warn", "approved": "b-ok", "rejected": "b-bad",
    "succeeded": "b-ok", "interrupted": "b-warn", "accept": "b-ok", "reject": "b-bad",
}
STATE_LABELS = {"working": "Working", "idle": "Idle", "waiting": "Waiting for input", "blocked": "Blocked",
                "awaiting_approval": "Awaiting approval", "under_review": "Under review", "failed": "Failed",
                "paused": "Paused", "offline": "Offline"}


@register.filter
def badge(status):
    return BADGES.get(str(status), "")


@register.filter
def state_label(state):
    return STATE_LABELS.get(state, state)


@register.filter
def get(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def initials(name):
    parts = str(name).split()
    return "".join(p[0] for p in parts[:2]).upper() or "?"


@register.filter
def pct(spent, limit):
    try:
        return min(100, round(100 * float(spent) / float(limit))) if float(limit) else 0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0
