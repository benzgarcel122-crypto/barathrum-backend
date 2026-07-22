from django.contrib.auth import logout as django_logout
from django.contrib.auth.decorators import login_required
# ...(rest of imports unchanged)

@login_required
@require_http_methods(["POST"])
def logout_view(request):
    """
    Logout, POST-only. Deliberately not GET: a GET-triggered logout is a well-known CSRF/link-
    prefetch footgun (a stray <a href> or an over-eager browser prefetch/crawler can silently log
    a user out). Template side calls this via a small {% csrf_token %} form + button, not a link.
    """
    django_logout(request)
    messages.info(request, "You've been logged out.")
    return redirect("login")
