from django.conf import settings
from django.shortcuts import redirect


def api_docs_redirect(request):
    """Send people to the branded docs while leaving OpenAPI machine-readable."""
    default_frontend = "http://localhost:5173" if settings.DEBUG else "https://sharptoolz.com"
    frontend_url = (settings.FRONTEND_URL or default_frontend).rstrip("/")
    return redirect(f"{frontend_url}/api-docs")
