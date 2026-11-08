from rest_framework.throttling import SimpleRateThrottle


class BurstRateThrottle(SimpleRateThrottle):
    scope = "burst"

    def get_cache_key(self, request, view):
        if request.user.is_authenticated:
            return self.cache_format % {"scope": self.scope, "ident": request.user.pk}
        # Scoped like the authenticated key: a bare IP would make the burst
        # and sustained throttles share one history, so the hourly limit
        # never applied to anonymous callers.
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class SustainedRateThrottle(SimpleRateThrottle):
    scope = "sustained"

    def get_cache_key(self, request, view):
        if request.user.is_authenticated:
            return self.cache_format % {"scope": self.scope, "ident": request.user.pk}
        # Scoped like the authenticated key: a bare IP would make the burst
        # and sustained throttles share one history, so the hourly limit
        # never applied to anonymous callers.
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AuthRateThrottle(SimpleRateThrottle):
    scope = "auth"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class PasswordResetThrottle(SimpleRateThrottle):
    scope = "password_reset"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AIGenerationThrottle(SimpleRateThrottle):
    scope = "ai_generation"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class PixelIngestThrottle(SimpleRateThrottle):
    scope = "pixel_ingest"

    def get_cache_key(self, request, view):
        pixel_key = request.data.get("pixel_key", self.get_ident(request))
        return self.cache_format % {"scope": self.scope, "ident": pixel_key}
