"""Every page requires authentication except an explicit public allowlist."""
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse

# /healthz/ready and /metrics are public paths but enforce their own token/Founder authorization.
PUBLIC_PREFIXES = ("/accounts/login/", "/accounts/password-reset/", "/accounts/reset/", "/healthz", "/metrics", "/static/")


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not request.path.startswith(PUBLIC_PREFIXES):
            if request.path.startswith("/api/"):
                return JsonResponse({"error": "authentication required"}, status=401)
            return redirect_to_login(request.get_full_path())
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """Content-Security-Policy with a per-request nonce for the app's own inline scripts, plus
    Permissions-Policy. Only the pinned Three.js CDN is allowed as an external script source."""

    SCRIPT_CDN = "https://unpkg.com"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        import secrets

        request.csp_nonce = secrets.token_urlsafe(16)
        response = self.get_response(request)
        host = request.get_host()
        policy = "; ".join([
            "default-src 'self'",
            f"script-src 'self' 'nonce-{request.csp_nonce}' {self.SCRIPT_CDN}",
            "style-src 'self' 'unsafe-inline'",  # inline style attributes only; no external styles
            "img-src 'self' data:",
            f"connect-src 'self' ws://{host} wss://{host}",
            "font-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ])
        response.headers.setdefault("Content-Security-Policy", policy)
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response
