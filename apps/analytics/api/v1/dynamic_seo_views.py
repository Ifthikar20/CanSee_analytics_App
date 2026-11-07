"""
Legacy per-site tracking script, kept so existing installs keep reporting.

New installs use the /p.js pixel (apps.websites.services.pixel_service).
This script used to also run an "SEO optimizer" that rewrote the host
page's meta tags and injected structured data; that half has been removed
and the script now only sends tracking events. Public (no auth) endpoint.
"""
from django.http import HttpResponse
from django.views import View

from apps.websites.models import Website


class SEOScriptView(View):
    """Serve the legacy tracking script for one website."""

    def get(self, request, website_id):
        try:
            website = Website.objects.get(id=website_id)
        except Website.DoesNotExist:
            return HttpResponse("// invalid site", content_type="application/javascript")

        track_endpoint = request.build_absolute_uri("/api/v1/track/")
        pixel_key = str(website.pixel_key)

        script = f"""
(function() {{
  'use strict';

  /* Cansee legacy tracking script. New installs use /p.js. */

  var FB = window.Cansee = window.Cansee || {{}};
  FB.siteId   = '{website_id}';
  FB.pixelKey = '{pixel_key}';
  FB.track    = '{track_endpoint}';
  FB.v        = '1.0';

  var sid = sessionStorage.getItem('fb_sid');
  if (!sid) {{ sid = Math.random().toString(36).substr(2, 12); sessionStorage.setItem('fb_sid', sid); }}
  FB.sessionId = sid;

  function send(eventType, extra) {{
    var payload = Object.assign({{
      pixel_key: FB.pixelKey,
      event_type: eventType,
      url: window.location.href,
      path: window.location.pathname,
      referrer: document.referrer,
      title: document.title,
      session_id: FB.sessionId,
      screen_w: screen.width,
      screen_h: screen.height,
      timestamp: new Date().toISOString()
    }}, extra || {{}});
    navigator.sendBeacon(FB.track + 'event/', JSON.stringify(payload));
  }}

  // Track pageview
  send('pageview');

  // Track clicks
  document.addEventListener('click', function(e) {{
    var t = e.target.closest('a, button, [data-fb-track]');
    if (t) {{
      send('click', {{
        element: t.tagName.toLowerCase(),
        text: (t.textContent || '').trim().slice(0, 100),
        href: t.href || '',
        x: e.clientX,
        y: e.clientY
      }});
    }}
  }});

  // Track scroll depth
  var maxScroll = 0;
  var scrollTimer;
  window.addEventListener('scroll', function() {{
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(function() {{
      var s = Math.round((window.scrollY / (document.body.scrollHeight - window.innerHeight)) * 100);
      if (s > maxScroll) {{
        maxScroll = s;
        if (maxScroll === 25 || maxScroll === 50 || maxScroll === 75 || maxScroll === 100) {{
          send('scroll', {{ depth: maxScroll }});
        }}
      }}
    }}, 200);
  }});

  // Track form submissions
  document.addEventListener('submit', function(e) {{
    var f = e.target;
    send('form_submit', {{ form_id: f.id || '', form_action: f.action || '' }});
  }});

  // Track time on page (send on unload)
  var pageStart = Date.now();
  window.addEventListener('beforeunload', function() {{
    send('time_on_page', {{ duration_ms: Date.now() - pageStart, max_scroll: maxScroll }});
  }});
}})();
"""
        response = HttpResponse(script.strip(), content_type="application/javascript")
        response["Cache-Control"] = "public, max-age=300"
        response["Access-Control-Allow-Origin"] = "*"
        return response
