"""User flow / path analysis."""
import logging
from collections import Counter, defaultdict

from django.db.models import Count

from apps.analytics.models import PageEvent
from core.utils.date_utils import get_date_range

logger = logging.getLogger("apps")


class FlowService:
    @staticmethod
    def get_user_flows(*, website_id: str, period: str = "30d", depth: int = 4) -> dict:
        """
        Aggregate page-to-page transitions for Sankey / flow visualization.
        Returns nodes (pages) and links (transitions with counts).
        """
        start, end = get_date_range(period)

        # Get pageview events ordered by visitor then timestamp
        events = (
            PageEvent.objects.filter(
                website_id=website_id,
                event_type="pageview",
                timestamp__range=(start, end),
            )
            .order_by("visitor_id", "timestamp")
            .values_list("visitor_id", "url")
        )

        # Build transitions
        transitions = Counter()
        current_visitor = None
        visitor_path = []

        for visitor_id, url in events:
            # Normalize URL to path only
            try:
                from urllib.parse import urlparse
                path = urlparse(url).path or "/"
            except Exception:
                path = url

            if visitor_id != current_visitor:
                # New visitor — process previous path
                FlowService._count_transitions(visitor_path, transitions, depth)
                current_visitor = visitor_id
                visitor_path = [path]
            else:
                if not visitor_path or visitor_path[-1] != path:
                    visitor_path.append(path)

        # Process last visitor
        FlowService._count_transitions(visitor_path, transitions, depth)

        # Build nodes and links
        all_pages = set()
        links = []
        for (source, target), count in transitions.most_common(50):
            all_pages.add(source)
            all_pages.add(target)
            links.append({"source": source, "target": target, "value": count})

        nodes = [{"id": p, "label": p} for p in sorted(all_pages)]

        return {
            "nodes": nodes,
            "links": links,
            "total_paths": sum(transitions.values()),
        }

    @staticmethod
    def _count_transitions(path, transitions, depth):
        """Count transitions in a visitor path up to depth steps."""
        for i in range(min(len(path) - 1, depth)):
            transitions[(path[i], path[i + 1])] += 1

    @staticmethod
    def get_entry_pages(*, website_id: str, period: str = "30d", limit: int = 10) -> list:
        """Top pages where visitors start their session, with source."""
        start, end = get_date_range(period)
        from apps.analytics.models import Session

        qs = (
            Session.objects.filter(
                visitor__website_id=website_id,
                started_at__range=(start, end),
            )
            .exclude(entry_page="")
            .values("entry_page", "source")
            .annotate(count=Count("id"))
            .order_by("-count")[:limit]
        )
        total = sum(r["count"] for r in qs) or 1
        return [
            {
                "page": r["entry_page"],
                "source": r["source"] or "direct",
                "count": r["count"],
                "pct": round(r["count"] / total * 100, 1),
            }
            for r in qs
        ]

    @staticmethod
    def get_exit_pages(*, website_id: str, period: str = "30d", limit: int = 10) -> list:
        """Top pages where visitors leave."""
        start, end = get_date_range(period)
        from apps.analytics.models import Session

        qs = (
            Session.objects.filter(
                visitor__website_id=website_id,
                started_at__range=(start, end),
            )
            .exclude(exit_page="")
            .values("exit_page")
            .annotate(count=Count("id"))
            .order_by("-count")[:limit]
        )
        total = sum(r["count"] for r in qs) or 1
        return [
            {
                "page": r["exit_page"],
                "count": r["count"],
                "pct": round(r["count"] / total * 100, 1),
            }
            for r in qs
        ]

    @staticmethod
    def get_visitor_journeys(*, website_id: str, period: str = "30d", limit: int = 20) -> list:
        """
        Return per-visitor session journeys with a heuristic intent score
        and page recommendations from converting sessions.
        """
        start, end = get_date_range(period)
        from collections import Counter

        from apps.analytics.models import Session

        # ── STEP 1: Analyze ALL sessions to build traffic model ──
        all_sessions = (
            Session.objects.filter(
                visitor__website_id=website_id,
                started_at__range=(start, end),
            )
            .select_related("visitor")
            .order_by("-started_at")
        )

        # Collect all session paths for statistical analysis
        all_paths = []          # list of page-path lists
        page_frequency = Counter()  # how often each page is visited across all sessions
        page_pairs = Counter()    # transition frequency: (from, to)
        conversion_pages = set()  # pages that indicate conversion intent
        converted_paths = []      # paths of sessions that reached a conversion page

        for sess in all_sessions:
            events = list(
                PageEvent.objects.filter(session=sess, event_type="pageview")
                .order_by("timestamp")
                .values_list("url", flat=True)
            )
            if not events:
                continue

            pages = []
            for url in events:
                try:
                    from urllib.parse import urlparse
                    path = urlparse(url).path or "/"
                except Exception:
                    path = url
                if not pages or pages[-1] != path:
                    pages.append(path)

            all_paths.append(pages)
            for p in pages:
                page_frequency[p] += 1

            # Build transition pairs
            for i in range(len(pages) - 1):
                page_pairs[(pages[i], pages[i + 1])] += 1

            # Detect conversion signals from actual behavior
            has_conversion = any(
                p for p in pages
                if any(kw in p.lower() for kw in
                       ("login", "signup", "register", "checkout", "subscribe",
                        "purchase", "thank", "confirm", "success", "account"))
            )
            if has_conversion:
                conversion_pages.update(pages)
                converted_paths.append(pages)

        # ── STEP 2: Compute page importance scores ──
        len(all_paths) or 1

        # Pages that appear in conversion paths but not just the conversion page itself
        conversion_correlated = Counter()
        for path in converted_paths:
            for p in path:
                conversion_correlated[p] += 1

        # Normalize: what % of converting sessions visited this page
        page_conversion_rate = {}
        total_converting = len(converted_paths) or 1
        for p, count in conversion_correlated.items():
            page_conversion_rate[p] = round(count / total_converting, 3)

        # Top pages by traffic (pages most visitors see)
        top_pages = [p for p, _ in page_frequency.most_common(20)]

        # Next best page predictions: for each page, what's the most likely next page
        next_page_prob = defaultdict(list)
        for (src, tgt), count in page_pairs.most_common():
            next_page_prob[src].append({"page": tgt, "probability": count})

        # ── STEP 3: Build enriched journeys ──
        journeys = []
        recent_sessions = list(all_sessions[:limit * 2])

        for sess in recent_sessions:
            events = list(
                PageEvent.objects.filter(session=sess, event_type="pageview")
                .order_by("timestamp")
                .values_list("url", flat=True)
            )
            if not events:
                continue

            pages = []
            for url in events:
                try:
                    from urllib.parse import urlparse
                    path = urlparse(url).path or "/"
                except Exception:
                    path = url
                if not pages or pages[-1] != path:
                    pages.append(path)

            duration_secs = 0
            if sess.ended_at and sess.started_at:
                duration_secs = int((sess.ended_at - sess.started_at).total_seconds())

            # ── Intent score (heuristic) ──
            # Score based on: depth, conversion correlation, page importance
            depth_score = min(len(pages) / 5.0, 1.0) * 30  # max 30 pts for depth
            conversion_score = sum(
                page_conversion_rate.get(p, 0) for p in pages
            ) / max(len(pages), 1) * 50  # max 50 pts for conversion correlation
            diversity_score = len(set(pages)) / max(len(top_pages[:10]), 1) * 20  # max 20 pts

            intent_score = round(min(depth_score + conversion_score + diversity_score, 100))

            # Classify based on score
            if intent_score >= 60:
                intent_cls = "dot-success"
                intent_label = f"High intent ({intent_score}%) - strong conversion signals"
            elif intent_score >= 40:
                intent_cls = "dot-info"
                intent_label = f"Evaluating ({intent_score}%) - exploring key pages"
            elif intent_score >= 20:
                intent_cls = "dot-warning"
                intent_label = f"Browsing ({intent_score}%) - moderate engagement"
            elif len(pages) <= 1:
                intent_cls = "dot-muted"
                intent_label = f"Bounce ({intent_score}%) - single page visit"
            else:
                intent_cls = "dot-neutral"
                intent_label = f"Low engagement ({intent_score}%) - brief visit"

            # ── Smart page recommendations ──
            # What pages do converting visitors see that this visitor missed?
            visited = set(pages)
            recommendations = []
            if converted_paths:
                # Find pages from conversion paths this visitor hasn't seen
                missed_from_conversions = []
                for p, rate in sorted(page_conversion_rate.items(), key=lambda x: -x[1]):
                    if p not in visited and rate > 0.3:  # pages in >30% of converting sessions
                        missed_from_conversions.append({
                            "page": p,
                            "reason": f"Seen in {round(rate * 100)}% of converting sessions"
                        })
                recommendations = missed_from_conversions[:3]

            # Next page prediction: based on their last page, where do most visitors go?
            last_page = pages[-1] if pages else "/"
            predicted_next = []
            if last_page in next_page_prob:
                for np_item in next_page_prob[last_page][:3]:
                    if np_item["page"] not in visited:
                        predicted_next.append(np_item["page"])

            v = sess.visitor
            journeys.append({
                "visitor_hash": (v.fingerprint_hash or "")[:12],
                "device": v.device_type or "",
                "country": v.geo_country or "",
                "browser": v.browser or "",
                "os": v.os or "",
                "pages": pages,
                "page_count": len(pages),
                "duration_secs": duration_secs,
                "source": sess.source or "direct",
                "started_at": sess.started_at.isoformat() if sess.started_at else None,
                # Heuristic fields
                "intent_score": intent_score,
                "intent_cls": intent_cls,
                "intent_label": intent_label,
                "recommendations": recommendations,
                "predicted_next": predicted_next[:2],
            })

            if len(journeys) >= limit:
                break

        return journeys
