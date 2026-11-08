"""Process liveness for load balancers: ``GET /healthz`` -> 200 "ok".

AWS load balancers (and container orchestrators) probe each target by its
private IP, so the Host header is an address that is not in ALLOWED_HOSTS
and Django would answer 400 DisallowedHost. This middleware sits first and
answers before anything reads the host. It checks nothing but that the
process serves requests: a database or Redis blip must not take every
target out of rotation at once. /health/ remains the full dependency check
for monitoring.
"""
from django.http import HttpResponse

LIVENESS_PATH = "/healthz"


class LivenessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info == LIVENESS_PATH and request.method in ("GET", "HEAD"):
            response = HttpResponse("ok", content_type="text/plain")
            response["Cache-Control"] = "no-store"
            return response
        return self.get_response(request)
