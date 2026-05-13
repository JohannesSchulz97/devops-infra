# devops-infra

Infrastructure-as-code for a self-hosted platform on a single Hetzner dedicated server — replacing Palantir Foundry with a fully owned, Git-driven stack running ~10 services for ~5 developers.

**Stack:** Docker · nginx · BorgBackup · Cloudflare R2 · GitHub Actions · Watchtower · Dagster · PostgreSQL (×7) · Beszel

---

## Problem

The org was paying ~€1.2M/year for Palantir Foundry and needed to migrate off it. That required a capable self-hosted platform to replace Foundry's data orchestration, workflow automation, CRM, and developer tooling — built from scratch, with no existing infrastructure.

---

## Solution

A single Hetzner dedicated server running all services as Docker containers, fronted by nginx with Let's Encrypt SSL. On push to `main`, GitHub Actions builds a Docker image and pushes it to GHCR. Watchtower polls GHCR every 5 minutes and automatically recreates any container whose image has been updated. **Deployments require no SSH — just a git push.**

This repo is the single source of truth for the server: Docker Compose stacks, nginx configs, systemd units, backup scripts, cron schedules, and a 470+ line runbook.

---

## Services

| Service | Domain | Stack | Notes |
|---------|--------|-------|-------|
| Twenty CRM | crm.\<host-domain\> | `stacks/twenty/` | Fork; custom CI build with auto-versioning |
| n8n | flow.\<host-domain\> | `stacks/n8n/` | Workflow automation |
| Dagster | dagster.\<host-domain\> | `stacks/dagster/` | 5 containers; prod/dev split at image level |
| SurfSense (fork) | oracle.\<host-domain\> | `stacks/surfsense/` | FastAPI backend + Next.js frontend, pgvector |
| LangGraph | langgraph.\<host-domain\> | `stacks/langgraph/` | Bearer token auth via nginx map directive |
| LiteLLM | llm.\<host-domain\> | `stacks/litellm/` | Unified LLM proxy; Google OAuth SSO |
| Listmonk | listmonk.\<host-domain\> | `stacks/listmonk/` | Fork; shares Twenty's PostgreSQL instance |
| Coder | coder.\<host-domain\> | systemd service | Browser/SSH workspaces for developers |
| Beszel | monitor.\<host-domain\> | `stacks/beszel/` | Monitoring hub + agent; Slack alerting |
| Watchtower | — | `stacks/watchtower/` | Auto-pulls updated images from GHCR |

All services sit behind nginx (80/443). All database ports are bound to `127.0.0.1` only.

---

## Architecture

### Server

| | |
|---|---|
| **Provider** | Hetzner Dedicated (AX41) |
| **CPU** | AMD EPYC-Milan, 16 cores |
| **RAM** | 64 GB |
| **Disk** | 338 GB SSD + 10 TB Storage Box (SSHFS, Hetzner BX21) |
| **OS** | Ubuntu 24.04.3 LTS |

### Deployment Pipeline

```
Developer pushes to GitHub repo
  → GitHub Actions builds Docker image
  → Pushes to GHCR (ghcr.io/<your-org>/<image>:latest)
  → Watchtower on server polls every 5 min
  → Detects new image digest, pulls, recreates container
  → Service updated — no SSH required
```

### Database Architecture

7 independent PostgreSQL instances, all bound to `127.0.0.1`:

| Port | Service | Notes |
|------|---------|-------|
| 5433 | SurfSense/Oracle | pgvector extension |
| 5434 | LangGraph | — |
| 5435 | Foundry datasets | ~1 TB, shared with Twenty stack |
| 5436 | Dagster | 3 databases: dagster, pipelines, pipelines_dev |
| 5437 | Twenty CRM + Listmonk | Shared instance |
| 5438 | n8n | — |
| 5432 | Coder | System-level instance |

One instance per service by design — a runaway query or migration in one service can't affect others.

### Dagster (most complex service)

5 containers: `dagster-db`, `dagster-webserver`, `dagster-daemon`, `dagster-user-code` (prod), `dagster-user-code-dev` (dev).

- **DockerRunLauncher** — each pipeline run gets its own isolated container
- **QueuedRunCoordinator** — max 4 concurrent runs
- **Prod/dev split at image level** — push to `main` → `:latest`; push to `dev` → `:dev`. The dev code location explicitly omits `TWENTY_POSTGRES_URL`, making production writes structurally impossible rather than just discouraged
- Dagster containers connect to Twenty's PostgreSQL via a shared Docker network for direct SQL writes

### Backup Architecture

```
Daily 04:00 — backup_pg.py
  → Auto-discovers running PostgreSQL containers via pg_isready
  → pg_dump / pg_dumpall per service
  → BorgBackup → Hetzner Storage Box (encrypted, deduplicated, zstd,3)
  → Writes JSON status files (Dagster can monitor these)

Daily 04:30 — backup_configs.py
  → Collects all docker-compose files, .env files, yaml configs from /opt/
  → BorgBackup → Hetzner Storage Box

Separate — backup_foundry_datasets_cfr2.py
  → pg_dump | gzip streamed directly to Cloudflare R2
  → S3 multipart upload (64 MB parts) — no temp file on disk
  → Required because the 1 TB dataset cannot be staged on the 338 GB root SSD
```

---

## Repository Structure

```
devops-infra/
├── stacks/          # Docker Compose per service
├── nginx/           # Reverse proxy site configs
├── systemd/         # Storage Box SSHFS mount unit, Coder service
├── scripts/         # Backup scripts (BorgBackup, R2, disk usage alerts)
├── cron/            # Cron job definitions
├── docs/            # Architecture doc, runbook, backup research
└── .env.example     # Backup credential template
```

---

## Key Decisions

**Single server, not Kubernetes**
Current scale (~10 services, ~5 developers) doesn't justify Kubernetes operational overhead. Isolation is achieved at the Docker/network level. Simpler to operate, easier to reason about, and cheaper.

**GHCR + Watchtower for deployments**
Closes the deployment loop without Ansible, SSH deploy scripts, or extra CI configuration. Developers need only a git push. The Makefile was deliberately removed once this pipeline was in place.

**BorgBackup to Hetzner Storage Box**
Deduplication is critical for daily dumps of large datasets. Encryption at rest. Storage Box is co-located with the server (fast writes, no egress). Evaluated and documented in `docs/research-hetzner-backup.md`.

**Cloudflare R2 for off-site Foundry dataset backup**
Zero egress fees vs AWS S3 or Backblaze B2. The Foundry dataset is ~1 TB compressed — egress costs with S3 would be significant at restore time. Streaming multipart upload avoids the disk staging requirement entirely. Documented in `docs/r2-backup-proposal.md`.

**systemd-journald as unified log driver**
All Docker containers log to journald. Searchable, retention-managed (2 GB / 90 days). Avoids a Loki/Grafana stack for this scale.

**Auto-discovering backup script**
Early `backup_pg.py` required manually registering every new PostgreSQL container. Refactored to auto-discover running containers via `pg_isready`, auto-initialize Borg repos on first use, and merge with a `PROFILE_OVERRIDES` dict for special cases.

---

## Notable Challenges

**Borg backups silently failing for 10 days**
All 6 backup targets failed with zero detection. Two root causes: (1) `.env` used `VAR=value` without `export` — child processes never received `BORG_PASSPHRASE`. (2) Backup cron runs as root, which has no SSH key — `BORG_RSH` needed an explicit `-i` flag. Both were silent: scripts logged errors, but nothing was monitoring the logs. Revealed the need for positive-confirmation alerting (alert on missing success, not just on errors).

**Docker services exposed directly to the internet**
Six services were bound to `0.0.0.0`, reachable by IP without TLS — since each was first deployed. Found during a dedicated server audit. Rebound all to `127.0.0.1`, removed the corresponding UFW rules.

**Disk space crisis — root SSD at 96%**
Docker artifacts (dangling images, build cache, orphaned volumes from old Coder workspaces and decommissioned services) had silently accumulated. `docker system prune` freed ~192 GB. Set up weekly auto-prune cron to prevent recurrence.

**Twenty CRM workspace recovery**
The Twenty workspace schema is created only by the `activateWorkspace` GraphQL mutation — not by the upgrade command. When the schema became corrupted, recovery required an 8-step procedure: dump users to CSV, drop and recreate the `core` schema, trigger workspace activation via GraphQL, re-insert users with bcrypt hashes, relink workspace membership. Now a 120-line runbook section.

**Listmonk cross-stack networking**
Listmonk shares Twenty's PostgreSQL but lives in a separate Docker Compose stack. First attempt used `host.docker.internal` — which fails because localhost inside a container refers to the container itself. Fix: define `crm-stack` as an explicit external Docker network shared between both stacks, then reference `twenty-db` by container name.

**Wildcard cert expiry — silent Cloudflare token revocation**
`*.coder.<host-domain>` was 6 days from expiry. Certbot renewal failed because the Cloudflare API token had silently lost its DNS zone permissions. Replaced with a new scoped token (Zone:Read + DNS:Edit for `<host-domain>`). Auto-renewal now works; documented in runbook.

---

## Outcomes

- ~5 developers deploying services without SSH access
- Daily automated backups of all databases; off-site copy of Foundry dataset (~1 TB)
- Prod/dev pipeline separation — dev pipelines structurally cannot write to production data
- All services bound to `127.0.0.1`; firewall restricted to ports 22/80/443
- Monitoring with Slack alerting (Beszel + custom disk usage script for extra volumes)
- Logging capped and rotated: journald 2 GB / 90 days, custom logs via logrotate
- Weekly Docker auto-prune preventing disk accumulation
- Server audit (March 2026): 12 issues resolved in one day, ~42 GB freed on root SSD
