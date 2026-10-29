"""Every page requires authentication except an explicit public allowlist."""
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse

PUBLIC_PREFIXES = ("/accounts/login/", "/accounts/password-reset/", "/accounts/reset/", "/healthz", "/static/")


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not request.path.startswith(PUBLIC_PREFIXES):
            if request.path.startswith("/api/"):
                return JsonResponse({"error": "authentication required"}, status=401)
            return redirect_to_login(request.get_full_path())
        return self.get_response(request)
