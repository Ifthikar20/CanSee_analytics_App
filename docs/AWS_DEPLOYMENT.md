# Deploying Cansee on AWS

Plan for moving production from one EC2 box running Docker Compose to AWS
managed services, with Postgres on RDS. Written from a read of this repo on
2026-11-08 (branch `claude/charming-euler-xqdxwo`). Prices are approximate
us-east-1 on-demand list prices; check the AWS pricing pages before buying.

## 1. Where production is today

| Piece | Today | Problem |
|---|---|---|
| Compute | One EC2 t3.small, Docker Compose (`docker/docker-compose.prod.yml`) | Single point of failure; 2 GB RAM shared by 9 containers |
| Database | Postgres 16 container on the same box | No managed backups, no failover, disk shared with the app |
| Redis | Container on the same box | Lost on box failure; cache and Celery broker share 100 MB |
| Web | gunicorn, 2 sync workers | |
| Workers | One container runs the Celery worker (concurrency 2) **and** beat | Cannot scale (beat would duplicate); long AI runs block emails |
| Agent | Optional uvicorn container, one process, in-memory events | Must stay a single replica |
| Frontend | Image on GHCR copied into a volume, served by nginx | Volume trick, `latest` tag, no rollback |
| Edge | Cloudflare (Full Strict) to nginx on 443 | Fine; keep Cloudflare |
| Deploy | GitHub Actions SSHes in, `git reset --hard`, builds on the box, migrates after the new code is live | No registry, no rollback, deploys can break the running app |
| Logs | stdout JSON + rotating files inside containers | Not shipped anywhere |
| Infra code | `infra/` Terraform: VPC, RDS, ElastiCache, S3, IAM | Compute, load balancer, ECR, CDN, secrets not written yet |

## 2. Target architecture

```
                 Cloudflare (DNS, TLS, WAF, CF-IPCountry)
                                 |
                  Application Load Balancer (ACM cert,
                  ingress from Cloudflare IP ranges only)
        /api/v1/agent/live/*  |          everything else
                 v            |                v
     ECS service: agent  (1)  |   ECS service: web (nginx + Django, 2+)
                              |      serves SPA, /api, /static, /p.js
                              |
     ECS service: worker-default (1+)   ECS service: worker-ai (1+)
     ECS service: worker-io (integrations, webhooks) (1)
     ECS service: beat (exactly 1)
     ECS one-off task: migrate + collectstatic (before every rollout)
                              |
        RDS Postgres 16 (private subnets, TLS required, PITR 7 days)
        ElastiCache Redis 7 (private subnets): db0 cache, db1 broker
        S3: backups, static assets, videos   CloudFront (optional, static)
        Secrets Manager: app secrets         SES: email   CloudWatch: logs, alarms
        ECR: images (built once in CI)       GitHub OIDC role for deploys
```

Recommended compute: **ECS on Fargate**. No servers to patch, rolling
deploys and rollbacks are built in, and each service scales on its own. The
cheaper first step (section 4, phase 1) keeps the EC2 box and moves only
the database.

The `intelligence` and `sources` FastAPI services can be dropped at first:
with `INTELLIGENCE_SERVICE_URL` and `SOURCES_SERVICE_URL` empty, the same
logic runs inside Django. Add them back as ECS services with Service
Connect when their load justifies it.

## 3. Code and config prerequisites

Done on this branch:

- [x] `STORAGES` for Django 5.1 (the old S3 lines in `prod.py` were ignored);
      `USE_S3_STORAGE=true` switches static files and uploads to S3.
- [x] `GET /healthz`: liveness that answers before `ALLOWED_HOSTS`, for ALB
      target checks by private IP. `/health/` stays the deep check.
- [x] `TRUSTED_PROXY_COUNT` drives client-IP detection and the login
      lockout key; set it to `2` behind Cloudflare -> ALB -> nginx -> Django.
- [x] Production refuses to start with the public default secret keys.
- [x] `core.tasks` registered with Celery (the scheduled hard delete ran never).
- [x] Polar 100/200-prompt product ids documented (checkout was 503 without them).

Still to do before ECS:

- [ ] **Split worker and beat.** Separate commands per service, no
      `bash -c "worker & beat & wait"` (it does not forward SIGTERM). Beat
      runs as exactly one task; workers scale per queue group.
- [ ] **Migrations as a one-off task** before the service rollout, never
      after the new code is live (today `deploy.sh` migrates afterwards).
- [ ] **Logs to stdout only** in prod: remove the RotatingFileHandlers in
      `prod.py` and `staging.py`; ECS ships stdout to CloudWatch.
- [ ] **Sentry**: `release=<git sha>` and environment from env (hardcoded
      today); add Sentry to the FastAPI services and the UI if wanted.
- [ ] **Bake the SPA into the web image** (nginx + `dist/`) instead of the
      shared volume, or host it on S3/CloudFront. If CloudFront, replace
      nginx's per-request CSP nonce (`sub_filter`) with hash-based CSP.
- [ ] **Pixel and maintenance page**: ship `docker/nginx/pixel/*` inside the
      web image; replace the maintenance flag file with an ALB fixed
      response rule.
- [ ] **Health check storage probe**: `health_check.storage` writes a file
      on every `/health/`; with S3 storage that is an S3 write per probe.
      Remove it or point it at local storage.
- [ ] **Agent process**: keep desired count 1 (in-memory event store and
      run pool). Before running more than one, move events and limits to
      Redis (noted in `apps/agent/runtime/events.py`).
- [ ] **Admin ops CIDR check** reads `REMOTE_ADDR`; behind an ALB that is
      the load balancer. Use the client IP helper instead.
- [ ] **Retire single-host scripts** (`scripts/logs.sh`, `run.sh`,
      `create_user.sh`, `create_prod_user.sh`, hardcoded IPs, `.pem` keys)
      in favour of ECS Exec or SSM Session Manager.

## 4. Migration plan (phased, each phase reversible)

### Phase 0: CI builds images (1 to 2 days)
- Add `ecr.tf` (repositories: web, worker, agent; optionally intelligence,
  sources) and a GitHub OIDC role so Actions pushes without stored keys.
- CI: lint, **pytest**, `manage.py check --deploy`, build once, push
  `:<git sha>`. The UI image builds with `npm ci` (lockfile) and all
  `VITE_*` build args.
- Deploys pull a SHA; rollback = redeploy the previous SHA.

### Phase 1: Postgres to RDS (1 to 2 days, about 15 minutes of downtime)
1. `terraform apply` the existing `infra/` (VPC, RDS, ElastiCache, S3, IAM).
2. The current box is in the default VPC and cannot reach RDS privately.
   Either start a replacement EC2 in the new VPC (recommended in
   `infra/README.md`) or peer the VPCs.
3. Decide on the RDS master password: `rds.tf` uses
   `manage_master_user_password = true`, which **rotates** the password and
   would break a static `DB_PASSWORD`. Create a separate app user with a
   static password stored in Secrets Manager, or turn rotation off.
4. Size connections: `CONN_MAX_AGE=600`, gunicorn workers, Celery processes
   and the thread pools in prompt box, insights, autopilot and country
   checks all hold connections. db.t4g.micro allows about 112. Start with
   db.t4g.small, or add RDS Proxy.
5. Cut over: maintenance mode, `pg_dump` from the container,
   `pg_restore` into RDS (or logical replication with the temporary
   Phase 4 security group rules for near-zero downtime), then set:
   ```
   DB_HOST=<rds endpoint>
   DB_SSLMODE=verify-full
   DB_SSLROOTCERT=/app/certs/rds-global-bundle.pem   # bake into the image
   DB_PASSWORD=<from Secrets Manager>
   ```
   `core.checks.check_database_ssl` refuses `prefer` for a remote host.
6. Verify (`/health/`, login, a prompt run), keep the old container stopped
   but intact for a week as the rollback, then remove `db` from Compose.

### Phase 2: Redis to ElastiCache (half a day)
- `REDIS_URL=redis://<endpoint>:6379/0`, `CELERY_BROKER_URL=redis://<endpoint>:6379/1`.
- The current Terraform has no in-transit TLS or AUTH. For TLS, switch to
  `aws_elasticache_replication_group` with transit encryption and an AUTH
  token, use `rediss://...?ssl_cert_reqs=CERT_REQUIRED`, and add the Celery
  `broker_use_ssl` option.
- Keep `maxmemory-policy noeviction` (a broker must not evict), or run two
  small clusters: cache (allkeys-lru) and broker (noeviction).

### Phase 3: ECS Fargate behind an ALB (3 to 5 days)
- Terraform: ECS cluster, task definitions and services per section 2,
  task and execution roles (today's IAM trusts EC2 only), `alb.tf` with an
  ACM certificate, target groups (`/healthz`), listener rules
  (`/api/v1/agent/live/*` -> agent), CloudWatch log groups.
- Networking: Fargate in private subnets needs a NAT gateway (AI provider
  calls are outbound) plus VPC endpoints for ECR, Logs, Secrets Manager.
  The cheaper start is public subnets with public IPs and security groups
  that allow only the ALB in.
- ALB security group: add Cloudflare's IPv6 ranges (only IPv4 today).
- Secrets: every value in `.env.prod` goes to Secrets Manager under
  `cansee-prod/*` (already allowed in `iam.tf`) and is injected through the
  task definition `secrets` block. No code change: settings read env vars.
- Set `TRUSTED_PROXY_COUNT=2`, `ALLOWED_HOSTS=cansee.ai,www.cansee.ai`.
- Point Cloudflare at the ALB, keep the EC2 box running until stable.

### Phase 4: Email, static, monitoring (1 to 2 days)
- **SES**: verify the domain with DKIM, request production access, create
  SMTP credentials, set `EMAIL_HOST=email-smtp.<region>.amazonaws.com`,
  `EMAIL_PORT=587`, `EMAIL_USE_TLS=true`. Email is broken until this is
  done (`core.E007`).
- **Static**: either keep serving from the web image (simplest) or
  `USE_S3_STORAGE=true` with CloudFront in front of the assets bucket and
  `AWS_S3_CUSTOM_DOMAIN=static.cansee.ai`. Move the landing videos to S3
  and set `VITE_ASSET_CDN`.
- **Alarms** (CloudWatch -> SNS email): ALB 5xx and unhealthy targets; RDS
  CPU, free storage, connections; ElastiCache memory; ECS task restarts; a
  queue-depth metric for scaling workers.
- **Backups**: RDS automated backups (7 days) and snapshots before each
  migration; S3 versioning for backups is already in `s3.tf`.

### Phase 5: Decommission
- Remove the EC2 box, the SSH deploy workflow and the `.pem` secrets,
  update `DEPLOY.md`, `docs/INFRASTRUCTURE.md` and `docs/ARCHITECTURE.md`
  (several sections describe Django Channels, which is not used).

## 5. Environment variables that change

| Variable | AWS value |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | RDS endpoint and app credentials (Secrets Manager) |
| `DB_SSLMODE`, `DB_SSLROOTCERT` | `verify-full`, path to the RDS CA bundle in the image |
| `REDIS_URL`, `CELERY_BROKER_URL` | ElastiCache endpoint, db 0 and db 1 |
| `ALLOWED_HOSTS` | `cansee.ai,www.cansee.ai` (no `localhost`; probes use `/healthz`) |
| `TRUSTED_PROXY_COUNT` | `2` |
| `USE_S3_STORAGE`, `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_CUSTOM_DOMAIN` | if static moves to S3 |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | **empty**: the task role supplies credentials |
| `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | SES SMTP |
| `INTELLIGENCE_SERVICE_URL`, `SOURCES_SERVICE_URL` | empty (in-process) or Service Connect names |
| `SENTRY_DSN` | as today; add release and environment |

Everything else (AI keys, Polar, OAuth, `FIELD_ENCRYPTION_KEY`,
`JWT_SIGNING_KEY`, `DJANGO_SECRET_KEY`) moves unchanged into Secrets Manager.

## 6. Monthly cost, rough

Small production, single AZ, on-demand, before free tier and data transfer:

| Service | Size | About |
|---|---|---|
| RDS Postgres | db.t4g.small, 20 GB gp3 | $25 to $30 (t4g.micro: about $14) |
| ElastiCache Redis | cache.t4g.micro | $12 |
| ALB | 1 | $17 plus usage |
| ECS Fargate | web 2 x (0.5 vCPU, 1 GB), agent 1 x (0.5, 1), workers 2 x (0.5, 1), beat 1 x (0.25, 0.5) | $95 to $110 |
| NAT gateway | 1 (if private subnets) | $33 plus $0.045/GB |
| S3, CloudFront, SES, Secrets Manager, CloudWatch | light use | $10 to $25 |
| **Total** | | **about $160 to $230/month** (about $130 without NAT) |

Multi-AZ RDS roughly doubles the database line. Today's single t3.small is
about $15/month: the difference buys managed backups, failover options,
zero-downtime deploys and room to scale.

## 7. Decisions needed

1. ECS Fargate (recommended) or EC2 Auto Scaling for compute.
2. Private subnets with NAT (more secure, +$33/month) or public subnets.
3. RDS master password rotation: separate static app user, or rotation off.
4. SPA from the web image (simplest) or S3 + CloudFront (needs CSP rework).
5. Keep Cloudflare in front (recommended: WAF, DNS, geo header) or move to
   CloudFront + AWS WAF.
6. Single-AZ to start, Multi-AZ RDS when revenue justifies it.
