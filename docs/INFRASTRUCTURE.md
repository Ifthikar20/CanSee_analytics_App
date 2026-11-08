# Cansee Infrastructure — How the Setup Works

Date: 2026-07-19
Host: EC2 `i-0464b3dd1b1d1c979` (cansee-prod), Elastic IP `100.55.196.80`
Instance type: t3.small (2 vCPU, 2 GB RAM), Ubuntu 22.04
App root on host: `/opt/cansee/ftb-api-26`
Compose file in use: `docker/docker-compose.prod.yml`

---

## 1. What is actually running

The entire production stack lives inside **one EC2 instance**, orchestrated by Docker Compose. Seven containers work together:

```
                            Cloudflare (edge cache, WAF, TLS)
                                       |
                                       | HTTPS
                                       v
    +----------------------------------------------------------------+
    |  EC2 t3.small  100.55.196.80                                   |
    |                                                                |
    |    +-------------+                                             |
    |    |   nginx     |  0.0.0.0:80 / 0.0.0.0:443                   |
    |    |  (docker-   |  Terminates TLS. Proxies to `web:8000`.     |
    |    |   nginx-1)  |  Only nginx is exposed to the internet.     |
    |    +------+------+                                             |
    |           |                                                    |
    |           v                                                    |
    |    +-------------+   +---------------+   +-----------------+   |
    |    | web:8000    |   | intelligence  |   | sources        |    |
    |    | Django/DRF  |   | :8000         |   | :8000          |    |
    |    | (primary    |   | (LLM heavy    |   | (scraping,     |    |
    |    |  API)       |   |  endpoints)   |   |  citations)    |    |
    |    +------+------+   +---------------+   +-----------------+   |
    |           |                                                    |
    |           | ORM writes + task publish                          |
    |           v                                                    |
    |    +------------------+       +-------------------------+      |
    |    | redis:6379       |<----->| celery                  |      |
    |    | DB0 = app cache  |       | (docker-celery-1)       |      |
    |    | DB1 = broker     |       | concurrency=2           |      |
    |    | (queue storage)  |       | + celery beat scheduler |      |
    |    +------------------+       | mem limit: 400 MB       |      |
    |                               +-----------+-------------+      |
    |                                           |                    |
    |                                           v                    |
    |                               Perplexity, OpenAI, SerpAPI,     |
    |                               Google Search  (outbound HTTPS)  |
    |                                                                |
    |    +------------------+                                        |
    |    | postgres:5432    |  All app data. Read/write by web,     |
    |    | (docker-db-1)    |  intelligence, sources, celery.       |
    |    +------------------+                                        |
    +----------------------------------------------------------------+
```

Only nginx has published ports. Everything else talks over Docker's private bridge network by container name (`web`, `redis`, `db`, etc.).

### Container roles

| Container | Purpose | Port (internal) |
|---|---|---|
| `docker-nginx-1` | Reverse proxy, TLS termination, static file serving | 80, 443 (published) |
| `docker-web-1` | Django API (auth, dashboard, most CRUD) | 8000 |
| `docker-intelligence-1` | Django API instance handling LLM-heavy endpoints | 8000 |
| `docker-sources-1` | Django API instance for citation/source scraping endpoints | 8000 |
| `docker-celery-1` | Celery worker + beat scheduler (all async work) | none |
| `docker-redis-1` | Broker for Celery (DB 1) + app cache (DB 0) | 6379 |
| `docker-db-1` | Postgres, persistent app database | 5432 |

The three `web`/`intelligence`/`sources` containers run the **same Django codebase** — they exist so nginx can route heavy endpoints away from the primary API tier. This is a soft separation, not a hard one; a bug in any of them can still saturate shared resources (Postgres, Redis).

---

## 2. The two request paths

Every user action falls into one of two categories. Understanding which is which explains almost all latency behavior.

### Sync path (fast, <1 second)

Anything the user expects to see immediately: page loads, list views, form submits, auth.

```
Browser  --->  Cloudflare  --->  nginx  --->  Django view  --->  Postgres
                                                 |
                                                 v
                                              response
Browser  <---  Cloudflare  <---  nginx  <---  Django view
```

Total wall-clock: 50-200 ms. The Django worker holds the connection open the whole time.

### Async path (slow, 30 seconds to several minutes)

Anything that involves external APIs (Perplexity, OpenAI) or heavy processing. The HTTP request returns in ~150 ms; the actual work happens later on a Celery worker.

```
Browser  --->  Django view
                    |
                    | 1. INSERT job row (status=pending)
                    | 2. task.delay(job_id)  --> Redis LPUSH into a queue
                    | 3. return 201 with job_id
                    v
                 response  --->  Browser

                                     (elsewhere, in a different process)

    Celery worker  ---  BRPOP  ---  Redis queue
         |
         | picks up job_id
         v
    Runs task function:
       - UPDATE status=running
       - call Perplexity / OpenAI / scrape URLs
       - INSERT result rows
       - UPDATE status=complete

    Meanwhile, browser polls  GET /jobs/{id}/  every 2 seconds
    until status == complete, then renders the result.
```

Search Insights, LLM ranking audits, brand security scans, and content briefs all use this path. If the browser is polling forever, the problem is almost always in the Celery half of the diagram.

---

## 3. End-to-end trace: Search Insights "Scan"

This is the feature we most recently debugged. It is a canonical async flow.

```
t=0.00s  Browser POST /api/v1/citations/websites/{id}/source-scans/
                 body: {"query": "best bagels in dallas"}

t=0.05s  nginx accepts, forwards to web:8000

t=0.10s  SourceScanListCreateView.post():
             - session auth        (SELECT user by session key)
             - tenant scope check  (verify website belongs to caller)
             - web_search.is_configured()  (checks PERPLEXITY_API_KEY)
             - active scan cap     (max 3 running per website)
             - INSERT SourceScan(status=pending)
             - run_source_scan.delay(scan.id)
                   --> Redis LPUSH into queue "ai"

t=0.15s  HTTP 201 back to browser. Total ~150 ms.

                    -- HTTP request is done here --

t=0.16s  Celery worker BRPOP on "ai" queue --> receives scan.id
t=0.17s  run_source_scan(scan_id):
             - UPDATE status=running
             - Perplexity API call (~10-30 s)
             - For each returned URL (typically 8-15):
                 * fetch page HTML
                 * LLM extract brands / sentiment / issues
             - UPDATE status=complete, write result rows

t=~60s   Done.

Meanwhile browser polls:
   GET /source-scans/{id}/  -> pending, results_count=0
   GET /source-scans/{id}/  -> running, results_count=3
   GET /source-scans/{id}/  -> running, results_count=7
   GET /source-scans/{id}/  -> complete, rows=[...]   render graph
```

**Key property:** the API responds in 150 ms whether the scan takes 10 s or 90 s. All slow work is on Celery. The web tier's job is only to accept the request, persist a marker, and hand it off.

### Task routing

Tasks are routed to named queues in `config/celery.py`:

```python
app.conf.task_routes = {
    "apps.websites.tasks.deliver_webhook": {"queue": "webhooks"},
    "apps.websites.tasks.refresh_expiring_tokens": {"queue": "integrations"},
    "apps.search_console.tasks.*": {"queue": "integrations"},
    "apps.llm_ranking.tasks.*": {"queue": "ai"},
    "apps.citations.tasks.*": {"queue": "ai"},
    "apps.brand_vault.tasks.*": {"queue": "ai"},
}
```

Queue topology:

- `default` — fast in-process work (analytics aggregation, pixel, accounts)
- `ai` — LLM-backed work (ranking audits, source scans, brand vault)
- `integrations` — third-party HTTP (OAuth token refresh, Search Console, SerpAPI, Polar, report digests) and long batch deletes
- `webhooks` — outbound webhook delivery to user-controlled URLs
- `high`, `low` — reserved for priority separation (currently unused)

**The worker must be started with `--queues=default,high,low,ai,integrations,webhooks`** or tasks routed to non-consumed queues sit in Redis forever.

---

## 4. How the app scales — 1 vs 10 vs 1000 users

The bottleneck moves as load grows. Numbers below assume all users hit the async Search Insights path (worst case).

### 1 user

- Web tier: idle (<0.1% CPU).
- Celery: task picked up in <100 ms.
- Postgres, Redis: idle.
- Latency dominated by Perplexity round-trip (~60 s).
- **Bottleneck: external API. Infra irrelevant.**

### 10 concurrent users

Web tier still fine. All 10 POSTs return 201 immediately.

But Celery is configured with **concurrency=2**. Only 2 tasks run in parallel; the other 8 wait in Redis.

```
Redis "ai" queue after 10 clicks:   [t1][t2][t3][t4][t5][t6][t7][t8][t9][t10]

Worker:  [slot 1: t1]  [slot 2: t2]         (running)
                          t3..t10 wait
```

With ~60 s per task:

| Task | Starts | Completes |
|---|---|---|
| t1, t2 | 0 s | 60 s |
| t3, t4 | 60 s | 120 s |
| t5, t6 | 120 s | 180 s |
| t7, t8 | 180 s | 240 s |
| t9, t10 | 240 s | 300 s |

User #10 waits 5 minutes for a 1-minute job. **Bottleneck: Celery concurrency.**

### 1000 users

Multiple systems fail in sequence:

1. **Celery backlog explodes.** 1000 tasks x 60 s / 2 concurrency = ~8.3 hours of queued work. Users see "pending" for hours, retry, and make it worse.
2. **Postgres connection pool saturates.** Every worker task holds an open DB connection. Default Postgres max is ~100 connections. Under burst, `too many connections` errors appear on *unrelated* endpoints (login, dashboard) because Postgres refuses new sessions.
3. **Memory pressure.** t3.small has 2 GB total. Seven containers plus per-task working memory pushes the OOM killer. Random containers get killed; typically Postgres or a web container drops, causing 500s across the board.
4. **CPU exhaustion.** t3.small uses burstable CPU credits. Sustained 100% load drains credits in ~30 minutes, then CPU is throttled to baseline (~40% of one core). Even sync pages get slow.
5. **External API rate limits.** Perplexity and OpenAI cap per-key throughput. At high concurrency, tasks fail with `429 Too Many Requests`.

**Bottleneck order at 1000 users:**
1. Celery concurrency (first to hit, always)
2. External API rate limits
3. Postgres connection pool
4. Instance memory
5. Instance CPU

### Sync-only pages scale differently

Pure DB reads (list past scans, dashboard summaries) do not touch Celery.

- 1 user: ~50 ms
- 10 users: ~50 ms each
- 1000 users: 200-500 ms if healthy, 5xx spikes when Postgres runs out of connections

Cloudflare page caching would absorb most of these before they reach origin, but caching is not currently configured for authenticated pages.

---

## 5. Realistic capacity today

Most active users are not running async jobs simultaneously — typically ~5% of active users have an in-flight scan at any given moment.

| Metric | Current capacity |
|---|---|
| Sync page views/sec (before Postgres pressure) | ~50-100 rps |
| Concurrent async jobs (Celery slots) | 2 |
| Time to drain 100 queued scans | ~50 min |
| Failure mode at overload | Silent queueing; users see spinner forever |

The single tightest number is **Celery concurrency = 2**. This is the ceiling on how many users can meaningfully use Search Insights, LLM ranking, or any AI feature at the same time.

---

## 6. Known infrastructure issues

### RESOLVED — Docker Compose `command:` used a YAML folded scalar incorrectly

Kept as a record because the failure mode is silent and easy to reintroduce.

The `celery` service in `docker/docker-compose.prod.yml` used a `>` folded scalar with
continuation lines indented deeper than the first content line. YAML preserves the
extra indentation as literal newlines, so `bash -c` received a multi-line script and
treated each line as a separate command. Bash ran `celery -A config.celery worker`
with no flags and blocked there, never reaching the rest.

**Symptom:** the worker consumed only the `default` queue, so anything routed to `ai`,
`integrations` or `webhooks` sat in Redis unprocessed and scans stayed `pending`
forever. `--concurrency=2` never applied. Celery beat never started at all, so no
scheduled task ran. Dev and CI were unaffected because both set
`CELERY_TASK_ALWAYS_EAGER`, which is why it survived so long.

**Fix applied:** the command is now a single-line `>-` scalar, with a comment above it
explaining why it must stay that way. Verify after any edit to that block:

```bash
python3 -c "import yaml; c=yaml.safe_load(open('docker/docker-compose.prod.yml'))['services']['celery']['command']; assert '\n' not in c; print(c)"
```

The `db` service uses the same folded style but is not affected — its command is a
plain argv list, and Compose shlex-splits it, so newlines collapse to whitespace. Only
a `bash -c "..."` wrapper turns the preserved newlines into separate commands.

Post-deploy confirmation:

```bash
docker compose -f docker/docker-compose.prod.yml exec celery celery -A config.celery inspect active_queues
# expect: default, high, low, ai, integrations, webhooks
docker compose -f docker/docker-compose.prod.yml logs celery | grep -i beat
# expect beat to have started
```

### Elastic IP is not attached at instance launch

When the current prod instance was launched, no Elastic IP was assigned. The instance came up on an auto-assigned public IP (`3.238.239.241`), while Cloudflare continued to resolve `cansee.ai` to the previous EIP `100.55.196.80`. Cloudflare returned 522 until the EIP was manually associated.

**Prevention:** in the EC2 launch config or Terraform, associate the EIP as part of instance creation, not as a manual post-step. Or use an ALB with a stable DNS name and point Cloudflare at that instead of a raw IP.

---

## 7. How to inspect the running system

All commands assume you have SSHed in:

```
ssh -i cansee-deploy.pem ubuntu@100.55.196.80
cd /opt/cansee/ftb-api-26
```

### Container health

```bash
sudo docker ps                                   # all containers, status, ports
sudo docker logs --tail 100 docker-web-1         # API request logs
sudo docker logs --tail 100 docker-celery-1      # worker + beat logs
sudo docker stats --no-stream                    # live CPU/memory per container
```

### Celery introspection

```bash
sudo docker exec docker-celery-1 celery -A config.celery inspect active_queues
sudo docker exec docker-celery-1 celery -A config.celery inspect active
sudo docker exec docker-celery-1 celery -A config.celery inspect registered
```

### Redis queue depths (broker is DB 1)

```bash
for q in default ai integrations webhooks high low; do
    echo -n "$q: "
    sudo docker exec docker-redis-1 redis-cli -n 1 LLEN "$q"
done
```

### Ad-hoc DB inspection via Django shell

```bash
sudo docker exec docker-web-1 python manage.py shell -c "
from apps.citations.models import SourceScan
from apps.websites.models import Website
w = Website.objects.get(id='<uuid>')
for s in SourceScan.objects.filter(website=w).order_by('-created_at')[:10]:
    print(s.id, s.status, s.query[:50])
"
```

### Nginx access log (all inbound requests)

```bash
sudo docker exec docker-nginx-1 tail -f /var/log/nginx/access.log
```

### Which commit is deployed

```bash
git -C /opt/cansee/ftb-api-26 rev-parse HEAD
```

---

## 8. Recommended next steps for capacity

Ordered by ratio of impact to effort:

1. Fix the celery `command:` YAML so all queues are consumed. **(Blocking bug; ~5 min.)**
2. Raise Celery concurrency from 2 to 8-16. Unlocks ~10-20 concurrent async jobs. **(One-line change.)**
3. Add a per-user rate limit on `POST /source-scans/` (e.g. 5/hour) to prevent retry storms. **(One decorator.)**
4. Move Postgres to RDS. Removes single-box coupling; makes vertical scaling of the EC2 non-destructive. **(2-hour migration.)**
5. Move Redis to ElastiCache. Same rationale. **(1 hour.)**
6. Split async workers onto a separate EC2 (or ECS service) behind the same Redis broker. Web tier and worker tier no longer compete for CPU/RAM. **(Half a day.)**
7. Add an autoscaling group for the worker tier so concurrency grows with the `ai` queue depth. **(One day, once step 6 is done.)**

Steps 1-3 alone are the difference between "the feature works for 2 users at a time" and "the feature works for ~30 users at a time" — likely enough headroom until active user count crosses a few hundred.
