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
│   └── default.conf                       # Default nginx config
├── systemd/                               # Custom systemd units
│   ├── mnt-storagebox.mount               # Hetzner Storage Box SSHFS mount
│   └── coder.service                      # Coder IDE server
├── scripts/
│   ├── backup_pg.py                       # pg_dump all databases → Storage Box via Borg
│   ├── backup_configs.py                  # /opt stack configs → Storage Box via Borg
│   └── backup_foundry_datasets_cfr2.py    # Foundry schema → Cloudflare R2
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
- **SSH user**: set via `SSH_USER` in `.env` (default: <admin-user>)

## Key Patterns

- Docker Compose stacks live in `/opt/<stack-name>/` on the server
- All services sit behind nginx with Let's Encrypt SSL (Certbot)
- Database ports are bound to 127.0.0.1 only (not publicly accessible)
- Backups use BorgBackup to Hetzner Storage Box (encrypted, deduplicated)
- Watchtower auto-updates images tagged with the watchtower label
- `.env` files contain secrets and are NOT committed — only `.env.example` templates

## PostgreSQL Instances

| Port | Container | Database | Used By |
|------|-----------|----------|---------|
| 5435 | foundry-datasets-db | default (foundry schema) | Foundry dataset backup |
| 5437 | twenty-db | default | Twenty CRM |
| 5436 | dagster-db | dagster, pipelines, pipelines_dev | Dagster |
| (internal) | n8n-db | n8n | n8n |
| 5433 | surfsense-db | surfsense | SurfSense + pgvector |
| 5434 | langgraph-db | langgraph | LangGraph |
| (internal) | listmonk_db | listmonk | Listmonk |

## Related Repositories

- **foundry-backup** — Palantir Foundry data extraction (finite project, being wound down)
- **dagster-pipelines** — Dagster pipeline code
- **llm-pipelines** — LangGraph agent definitions
- **listmonk** — Custom Listmonk fork
- **oracle-backend / oracle-frontend** — SurfSense customization

## Working Instructions

- **Keep CLAUDE.md current**: Update this file if infrastructure changes.
- **Never commit secrets**: `.env` files, passwords, API keys stay out of git.
- **Test compose changes locally**: `docker compose config` validates syntax before deploying.
- Docker Compose files in this repo are **reference copies** — the live versions are on the server in `/opt/`. After editing, deploy changes to the server.
