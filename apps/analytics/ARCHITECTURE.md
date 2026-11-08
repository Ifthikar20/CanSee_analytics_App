# Analytics App

## Purpose

The core data collection and analysis engine. Ingests behavioral data from the tracking pixel, tracks keyword rankings, monitors competitors, and surfaces AI-powered insights.

## Architecture

```
Website (with pixel installed)
  │
  ├── pixel/cansee.min.js  → Collects pageviews, clicks, scroll, forms
  │     │
  │     ▼
  │   POST /api/v1/track/      → High-throughput pixel ingestion endpoint
  │     │
  │     ▼
  │   Visitor → Session → PageEvent (raw event storage)
  │
  ├── Keyword Tracking
  │     TrackedKeyword → KeywordRankHistory (daily snapshots)
  │     CompetitorDomain → CompetitorKeywordRank
  │     KeywordAlert → KeywordAlertEvent (threshold-based notifications)
  │
  ├── DOM Scanning
  │     KeywordScanConfig → automatic periodic re-scans
  │
  └── Aggregation (Celery)
        aggregate_hourly_metrics → pre-computed dashboard data
```

## Models

| Model | Purpose |
|---|---|
| `Visitor` | Unique visitor identified by fingerprint hash. Stores device info, geo, visit count, lead score. Privacy-first: IPs are hashed, never stored raw. |
| `Session` | A browsing session with entry/exit pages, UTM params (source, medium, campaign), and page count. |
| `PageEvent` | Raw events: pageview, click, form_submit, scroll, session_end, custom. Includes scroll depth and time-on-page. |
| `CustomFunnel` | User-defined conversion funnels with JSON step definitions. |
| `TrackedKeyword` | Keywords being monitored for rank changes. Stores current/previous/best rank, search volume, and difficulty. |
| `KeywordRankHistory` | Daily rank snapshots with SERP features data. |
| `TrackedLink` | Short tracked URLs for campaign attribution. Records click and conversion counts. |
| `LinkClick` | Individual click events on tracked links, with visitor attribution and conversion tracking. |
| `KeywordScanConfig` | Per-website DOM scan settings: interval (hourly to weekly), depth, and scheduling. |
| `PlatformContent` | Social media posts with auto-extracted keywords for cross-platform keyword gap analysis. |
| `KeywordAlert` | Alert rules that fire when keywords move beyond a threshold, with direction filtering. |
| `KeywordAlertEvent` | Individual alert firings with old/new rank and change magnitude. |
| `CompetitorDomain` | Competitor domains to track keyword rankings against. |
| `CompetitorKeywordRank` | Point-in-time keyword rank for a competitor domain. |

## Services

| Service | Purpose |
|---|---|
| `AnalyticsService` | Dashboard overview, top pages, traffic breakdowns |
| `AggregationService` | Pre-aggregates hourly metrics for fast dashboard reads |
| `KeywordIntelligenceService` | AI-scored keywords, trending analysis |
| `AIInsightsService` | AI-generated anomaly detection and opportunity insights |
| `DataForSEOService` | External API integration for keyword enrichment |

## Celery Tasks

| Task | Schedule | Purpose |
|---|---|---|
| `aggregate_hourly_metrics` | Every hour (at :05) | Pre-aggregate analytics for all active websites |
| `check_keyword_alerts` | Periodic | Evaluate alert rules and fire notifications for keyword rank changes |
| `check_competitor_rankings` | On-demand | Fetch DataForSEO rankings for a competitor domain |

## Key Design Decisions

- **Privacy-first tracking** — Visitor IPs are hashed (`ip_hash`), never stored raw. Fingerprinting uses a hash for deduplication without PII.
- **High-throughput ingestion** — The pixel endpoint is separate from the main API (`/api/v1/track/`) for independent scaling.
- **Pre-aggregation** — Hourly aggregation via Celery ensures the dashboard reads pre-computed data, not raw events.
- **No tracked-link redirect** — The `/t/<tracking_key>/` redirect was removed. The `TrackedLink` and `LinkClick` tables are kept for existing rows; only the admin and the retention purge still read them.

## Dependencies

- **Depends on:** `websites`, `leads` (campaign recipient attribution), `core`
- **External:** DataForSEO API, Google Trends
