class SecurityHeadersMiddleware:
    """
    SOC2 Control: CC6.6 — Defense against common web attacks.
    Adds all recommended security headers to every response.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        response["X-Content-Type-Options"] = "nosniff"
        response["X-Frame-Options"] = "DENY"
        response["X-XSS-Protection"] = "1; mode=block"
        response["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self' wss:; "
            "frame-src 'none'; "
            "object-src 'none'; "
            "base-uri 'self';"
        )
        cache_control = "no-store, no-cache, must-revalidate, private"
        if response.get("Content-Type", "").startswith("text/event-stream"):
            # Proxies must not buffer, compress or rewrite an event stream.
            cache_control += ", no-transform"
        response["Cache-Control"] = cache_control
        response["Pragma"] = "no-cache"

        return response
