# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

This repository manages the infrastructure for the <your-org> main server (`<server-host>`, <server-ip>) — a Hetzner dedicated server running Ubuntu 24.04. It contains Docker Compose stacks, nginx configs, systemd units, and backup scripts.

## Repository Structure

```
devops-infra/
├── CLAUDE.md                              # This file
├── README.md                              # Server overview, quick start
├── .env.example                           # Credentials template (SSH_USER, backups)
├── stacks/                                # Docker Compose per service
│   ├── twenty/docker-compose.yml          # Twenty CRM (crm.<host-domain>)
│   ├── listmonk/docker-compose.yml        # Listmonk email marketing
│   ├── n8n/docker-compose.yml             # n8n automation (flow.<host-domain>)
│   ├── dagster/                           # Dagster orchestration (dagster.<host-domain>)
│   │   ├── docker-compose.yml
│   │   ├── dagster.yaml
│   │   └── workspace.yaml
│   ├── surfsense/docker-compose.yml       # SurfSense/Oracle (oracle.<host-domain>)
│   ├── langgraph/                         # LangGraph agents (langgraph.<host-domain>)
│   │   ├── docker-compose.yml
│   │   └── docker-compose.prod.yml
│   ├── beszel/docker-compose.yml          # Beszel monitoring hub + agent (monitor.<host-domain>)
│   └── watchtower/docker-compose.yml      # Auto image updater
├── nginx/                                 # Reverse proxy site configs
│   ├── twenty.conf                        # crm.<host-domain> → :3003
│   ├── n8n.conf                           # flow.<host-domain> → :5678
│   ├── dagster.conf                       # dagster.<host-domain> → :3010
│   ├── surfsense.conf                     # oracle.<host-domain> → :8000/:3001
│   ├── langgraph.conf                     # langgraph.<host-domain> → :8123
│   ├── coder.conf                         # coder.<host-domain> → :3000
│   ├── vibekanban.conf                    # vibekanban.<host-domain> → :8082
│   ├── api-vibekanban.conf                # api.vibekanban.<host-domain> → :8081
│   ├── beszel.conf                        # monitor.<host-domain> → :8090
│   └── default.conf                       # Default nginx config
├── systemd/                               # Custom systemd units
│   ├── journald.conf                      # Journald log cap (2G / 90d)
│   ├── mnt-storagebox.mount               # Hetzner Storage Box SSHFS mount
│   └── coder.service                      # Coder IDE server
├── scripts/
│   ├── backup_pg.py                       # pg_dump all databases → Storage Box via Borg
│   ├── backup_configs.py                  # /opt stack configs → Storage Box via Borg
│   ├── backup_foundry_datasets_cfr2.py    # Foundry schema → Cloudflare R2
│   └── check_disk_usage.sh               # Disk threshold alerts → Slack (cron every 6h)
└── docs/
    ├── architecture.md                    # Port map, disk layout, PG instances, topology
    ├── runbook.md                         # Common operations reference
    ├── r2-backup-proposal.md              # R2 backup strategy research
    └── research-hetzner-backup.md         # Hetzner backup research
```

## Server Details

- **Host**: <server-host> (<server-ip>)
- **OS**: Ubuntu 24.04.3 LTS
- **CPU**: AMD EPYC-Milan, 16 cores
- **RAM**: 64 GB
- **Disk**: 338 GB SSD + 10 TB Storage Box (SSHFS at /mnt/storagebox)
- **SSH users**: each person has their own OS account — `SSH_USER` in `.env` must match their account name

## Server OS Accounts

Each user has a personal OS account and SSH key. `<admin-user>` has sudo/admin rights and is reserved for infrastructure admins only.

| User | Account | Access level |
|------|---------|-------------|
| dev1 Schulz | `dev1` | admin (sudo) |
| dev10 Soyka | `dev10` | standard |
| dev10-Jean | `dev10-jean` | standard |
| dev10 | `dev10` | standard |
| dev10 | `dev10` | standard |
| dev10 | `dev10` | standard |
| dev3 | `dev3` | standard |

SSH keys live in `/home/<user>/.ssh/authorized_keys`. When adding a new user's machine, always put the key in their own account, never in `<admin-user>`.

**`SSH_USER` in `.env`** must be set to the user's own account name (e.g. `dev10`, `dev1`), not `<admin-user>` unless they are an infra admin.

## Key Patterns

- Docker Compose stacks live in `/opt/<stack-name>/` on the server
- All services sit behind nginx with Let's Encrypt SSL (Certbot)
- Database ports are bound to 127.0.0.1 only (not publicly accessible)
- Backups use BorgBackup to Hetzner Storage Box (encrypted, deduplicated)
- Watchtower auto-updates images tagged with the watchtower label (`WATCHTOWER_CLEANUP=true` removes old image versions automatically)
- Docker uses the **journald** log driver — container logs go to systemd-journald (query with `journalctl CONTAINER_NAME=<name>`). Journal is capped at 2 GB / 90 days (see `/etc/systemd/journald.conf`)
- `.env` files contain secrets and are NOT committed — only `.env.example` templates
- Docker data root is at the default `/var/lib/docker` on the root SSD — **TODO: move to `/mnt/main/docker`** to prevent disk pressure

## Disk Management

- **Root SSD** (338 GB): OS, Docker images/layers/cache (`/var/lib/docker`)
- **`/mnt/main`** (4.9 TB, `/dev/sdb`): PostgreSQL data dirs (`pg_*`), foundry mediasets
- **`/mnt/storagebox`** (10 TB): Borg backup target

### Weekly Docker prune cron

A cron job runs every Sunday at 3 AM to remove dangling images, stopped containers, unused networks, and build cache:

```
/etc/cron.d/docker-prune:
0 3 * * 0 root docker system prune -f >> /var/log/docker-prune.log 2>&1
```

Output is logged to `/var/log/docker-prune.log`. Note: named volumes are NOT pruned by this job — those require manual review (`docker volume ls --filter dangling=true`).

### Disk usage alerts cron

A cron job runs every 6 hours to check filesystem usage and alert to Slack:

```
/etc/cron.d/check-disk-usage:
0 */6 * * * root /opt/scripts/check_disk_usage.sh >> /var/log/check-disk-usage.log 2>&1
```

Thresholds: `/mnt/main` > 80%, `/mnt/storagebox` > 70%, `/` > 90%. Alerts go to the same Slack webhook as Beszel. This is a stopgap until Beszel supports extra filesystem alerts natively.

## PostgreSQL Instances

| Port | Container | Database | Used By |
|------|-----------|----------|---------|
| 5435 | foundry-datasets-db | default (foundry schema) | Foundry dataset backup |
| 5437 | twenty-db | default | Twenty CRM |
| 5436 | dagster-db | dagster, pipelines, pipelines_dev | Dagster |
| 5438 | n8n-db | n8n | n8n |
| 5433 | surfsense-db | surfsense | SurfSense + pgvector |
| 5434 | langgraph-db | langgraph | LangGraph |
| (internal) | listmonk_db | listmonk | Listmonk |

### Read-only DB access

PostgreSQL users `dev10_readonly` and `dev1_readonly` exist on all 5 exposed DBs (Twenty, Dagster, Surfsense, Foundry, n8n). These users have SELECT-only privileges — no INSERT, UPDATE, DELETE, DROP, or CREATE possible.

Access flow: SSH key → personal OS account → SSH tunnel → `*_readonly` PG user.

When granting DB access to a new person:
1. Add their SSH key to `/home/<their-account>/.ssh/authorized_keys` (never to `<admin-user>`)
2. Create a `<name>_readonly` PostgreSQL user on each DB container with SELECT-only grants
3. Never grant write privileges to non-admin users

## Related Repositories

- **foundry-backup** — Palantir Foundry data extraction (finite project, being wound down)
- **dagster-pipelines** — Dagster pipeline code
- **llm-pipelines** — LangGraph agent definitions
- **listmonk** — Custom Listmonk fork
- **oracle-backend / oracle-frontend** — SurfSense customization

## Beszel Alerting

- Notification channel: Slack incoming webhook (Shoutrrr format)
- Shoutrrr URL (stored in `.env` as `BESZEL_SLACK_WEBHOOK`): `slack://hook:<slack-webhook-token>@webhook`
- Admin credentials stored in `.env` as `BESZEL_ADMIN_EMAIL` / `BESZEL_ADMIN_PASSWORD`

### Configured Alerts (<server-host>)

| Alert | Threshold | Min Duration |
|-------|-----------|-------------|
| Status | — | Immediate |
| CPU | > 80% | 5 min |
| Memory | > 85% | 5 min |
| Disk | > 80% | Immediate |
| Temperature | > 80°C | Immediate |
| LoadAvg5 | > 13 (80% of 16 cores) | 5 min |

### API Interaction

Beszel uses PocketBase — alerts are managed via its REST API on the server at `http://localhost:8090` (not exposed publicly, access via SSH).

**Auth — must use the `users` collection (not `_superusers`) to pass alert ownership validation:**
```python
# Authenticate
POST /api/collections/users/auth-with-password
{"identity": "<email>", "password": "<password>"}
# Returns token for subsequent requests

# To reset user password (requires superuser token first):
POST /api/collections/_superusers/auth-with-password → get sutoken
PATCH /api/collections/users/records/<user_id> → set new password
```

**Key collections:**
- `user_settings` — notification URLs (`settings.webhooks[]` Shoutrrr URLs, `settings.emails[]`)
- `alerts` — per-system thresholds (`user`, `system`, `name`, `value`, `min` fields)
- `systems` — monitored systems

**Preferred approach for scripting:** run a Python script via SSH rather than curl (avoids shell escaping issues with nested JSON):
```bash
ssh <admin-user>@<server-ip> "python3 << 'EOF'
import urllib.request, json
# ... use urllib to hit http://localhost:8090
EOF"
```

## Working Instructions

- **Keep CLAUDE.md current**: Update this file if infrastructure changes.
- **Never commit secrets**: `.env` files, passwords, API keys stay out of git.
- **Test compose changes locally**: `docker compose config` validates syntax before deploying.
- Docker Compose files in this repo are **reference copies** — the live versions are on the server in `/opt/`. After editing, deploy changes to the server.
