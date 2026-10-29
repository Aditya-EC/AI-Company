"""Login rate limiting keyed by IP and by username (cache-backed)."""
from django.conf import settings
from django.core.cache import cache


def _keys(ip, username):
    return [f"login-fail:ip:{ip}", f"login-fail:user:{(username or '').lower()}"]


def is_limited(ip, username):
    limit = settings.VCOOS_LOGIN_RATE_LIMIT
    return any((cache.get(k) or 0) >= limit for k in _keys(ip, username))


def register_failure(ip, username):
    for k in _keys(ip, username):
        try:
            cache.incr(k)
        except ValueError:
            cache.set(k, 1, settings.VCOOS_LOGIN_RATE_WINDOW)


def reset(ip, username):
    cache.delete_many(_keys(ip, username))
