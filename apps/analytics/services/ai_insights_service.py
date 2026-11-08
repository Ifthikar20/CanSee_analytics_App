"""Rule-based analytics insights: anomaly detection and suggested actions."""
import logging
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from apps.analytics.models import PageEvent, Session, Visitor
from core.utils.date_utils import get_date_range

logger = logging.getLogger("apps")


class AIInsightsService:
    @staticmethod
    def generate_insights(*, website_id: str, period: str = "7d") -> list:
        """Generate insight cards from rule-based checks on the site's own data."""
        insights = []

        # 1. Anomaly detection — compare recent period to baseline
        anomalies = AIInsightsService.detect_anomalies(website_id=website_id)
        insights.extend(anomalies)

        # 2. Traffic pattern insights
        patterns = AIInsightsService._analyze_traffic_patterns(website_id=website_id, period=period)
        insights.extend(patterns)

        # 3. Content performance insights
        content = AIInsightsService._analyze_content(website_id=website_id, period=period)
        insights.extend(content)

        # 4. Engagement insights
        engagement = AIInsightsService._analyze_engagement(website_id=website_id, period=period)
        insights.extend(engagement)

        # Sort by priority
        priority_order = {"critical": 0, "warning": 1, "opportunity": 2, "info": 3}
        insights.sort(key=lambda x: priority_order.get(x.get("type", "info"), 99))

        return insights[:12]  # Cap at 12 insights

    @staticmethod
    def detect_anomalies(*, website_id: str) -> list:
        """Compare today's metrics vs 7-day average. Flag significant deviations."""
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = today_start - timedelta(days=7)

        insights = []

        # Today's pageviews
        today_pv = PageEvent.objects.filter(
            website_id=website_id,
            event_type="pageview",
            timestamp__gte=today_start,
        ).count()

        # 7-day daily average
        week_pv = PageEvent.objects.filter(
            website_id=website_id,
            event_type="pageview",
            timestamp__range=(week_ago, today_start),
        ).count()
        avg_pv = week_pv / 7 if week_pv else 0

        if avg_pv > 0:
            ratio = today_pv / avg_pv
            if ratio >= 1.5:
                insights.append({
                    "type": "opportunity",
                    "icon": "trend-up",
                    "title": "Traffic Spike Detected",
                    "description": f"Today's pageviews ({today_pv}) are {round(ratio, 1)}x your 7-day average ({round(avg_pv)}). Something is driving traffic — check your sources.",
                    "action": "View traffic sources to identify what's working",
                    "metric": f"+{round((ratio - 1) * 100)}%",
                })
            elif ratio <= 0.5 and avg_pv >= 5:
                insights.append({
                    "type": "warning",
                    "icon": "trend-down",
                    "title": "Traffic Drop Detected",
                    "description": f"Today's pageviews ({today_pv}) are only {round(ratio * 100)}% of your 7-day average ({round(avg_pv)}). Check for issues.",
                    "action": "Check that the site is up and the tracking pixel is still installed",
                    "metric": f"-{round((1 - ratio) * 100)}%",
                })

        # Today's unique visitors vs average
        today_vis = Visitor.objects.filter(
            website_id=website_id,
            last_seen__gte=today_start,
        ).count()

        week_vis = Visitor.objects.filter(
            website_id=website_id,
            last_seen__range=(week_ago, today_start),
        ).count()
        avg_vis = week_vis / 7 if week_vis else 0

        if avg_vis > 0 and today_vis / avg_vis >= 1.5:
            insights.append({
                "type": "opportunity",
                "icon": "new",
                "title": "New Visitor Surge",
                "description": f"You have {today_vis} unique visitors today vs your daily average of {round(avg_vis)}. New audiences are discovering your site.",
                "action": "Check which pages they're landing on",
                "metric": f"+{round((today_vis / avg_vis - 1) * 100)}%",
            })

        return insights

    @staticmethod
    def _analyze_traffic_patterns(*, website_id: str, period: str) -> list:
        """Analyze traffic source patterns."""
        start, end = get_date_range(period)
        insights = []

        # Source distribution
        sources = (
            Session.objects.filter(
                visitor__website_id=website_id,
                started_at__range=(start, end),
            )
            .values("source")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        source_list = list(sources)
        if source_list:
            total = sum(s["count"] for s in source_list)
            top = source_list[0]
            top_pct = round(top["count"] / total * 100) if total else 0

            if top_pct > 70:
                insights.append({
                    "type": "warning",
                    "icon": "alert",
                    "title": "Over-Reliance on Single Traffic Source",
                    "description": f"{top_pct}% of your traffic comes from '{top['source'] or 'direct'}'. Diversify your channels to reduce risk.",
                    "action": "Consider investing in SEO, social media, or paid acquisition",
                    "metric": f"{top_pct}%",
                })

            # Check for no organic traffic
            organic = next((s for s in source_list if "google" in (s["source"] or "").lower() or "organic" in (s["source"] or "").lower()), None)
            if not organic and total > 10:
                insights.append({
                    "type": "opportunity",
                    "icon": "search",
                    "title": "No Organic Search Traffic",
                    "description": "You're not getting any traffic from search engines. SEO optimization could unlock a major growth channel.",
                    "action": "Check where your pages rank on Google for the prompts you track",
                    "metric": "0%",
                })

        return insights

    @staticmethod
    def _analyze_content(*, website_id: str, period: str) -> list:
        """Analyze content performance."""
        start, end = get_date_range(period)
        insights = []

        # Top pages by views
        top_pages = (
            PageEvent.objects.filter(
                website_id=website_id,
                event_type="pageview",
                timestamp__range=(start, end),
            )
            .values("url")
            .annotate(views=Count("id"))
            .order_by("-views")[:5]
        )

        pages = list(top_pages)
        if len(pages) >= 2:
            # Check concentration — if #1 page has 5x+ more than #2
            if pages[0]["views"] >= pages[1]["views"] * 5:
                insights.append({
                    "type": "info",
                    "icon": "content",
                    "title": "Content Concentration",
                    "description": f"Your top page gets {pages[0]['views']} views while the next page gets only {pages[1]['views']}. Create more content to distribute traffic.",
                    "action": "Create content similar to your top-performing page",
                    "metric": f"{pages[0]['views']} views",
                })

        return insights

    @staticmethod
    def _analyze_engagement(*, website_id: str, period: str) -> list:
        """Analyze user engagement metrics."""
        start, end = get_date_range(period)
        insights = []

        # Average scroll depth
        scroll_events = PageEvent.objects.filter(
            website_id=website_id,
            event_type="scroll",
            timestamp__range=(start, end),
        )
        if scroll_events.exists():
            deep_scrolls = scroll_events.filter(properties__depth__gte=75).count()
            total_scrolls = scroll_events.count()
            deep_pct = round(deep_scrolls / total_scrolls * 100) if total_scrolls else 0

            if deep_pct < 20 and total_scrolls > 10:
                insights.append({
                    "type": "warning",
                    "icon": "scroll",
                    "title": "Low Scroll Engagement",
                    "description": f"Only {deep_pct}% of visitors scroll past 75% of your pages. Content above the fold may not be compelling enough.",
                    "action": "Improve above-the-fold content and add compelling CTAs higher on the page",
                    "metric": f"{deep_pct}%",
                })
            elif deep_pct > 60:
                insights.append({
                    "type": "info",
                    "icon": "star",
                    "title": "Excellent Scroll Depth",
                    "description": f"{deep_pct}% of visitors read 75%+ of your content. Your content strategy is working well.",
                    "action": "Keep creating similar content and add CTAs at scroll depth sweet spots",
                    "metric": f"{deep_pct}%",
                })

        # Click activity
        click_count = PageEvent.objects.filter(
            website_id=website_id,
            event_type="click",
            timestamp__range=(start, end),
        ).count()

        pv_count = PageEvent.objects.filter(
            website_id=website_id,
            event_type="pageview",
            timestamp__range=(start, end),
        ).count()

        if pv_count > 0:
            clicks_per_view = round(click_count / pv_count, 1)
            if clicks_per_view < 1 and pv_count > 20:
                insights.append({
                    "type": "warning",
                    "icon": "click",
                    "title": "Low Click Engagement",
                    "description": f"Visitors average only {clicks_per_view} clicks per pageview. Consider adding more interactive elements and clearer CTAs.",
                    "action": "Add clearer calls to action on your most-viewed pages",
                    "metric": f"{clicks_per_view}/view",
                })

        return insights

    @staticmethod
    def suggest_actions(*, website_id: str) -> list:
        """Generate prioritized action recommendations."""
        insights = AIInsightsService.generate_insights(website_id=website_id, period="7d")

        actions = []
        for insight in insights:
            if insight.get("action"):
                actions.append({
                    "priority": insight["type"],
                    "action": insight["action"],
                    "reason": insight["title"],
                    "impact": insight.get("metric", ""),
                })

        return actions
