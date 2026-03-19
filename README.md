# devops-infra

Infrastructure-as-code for the <your-org> main server (`<server-host>`, <server-ip>).

## Server Overview

| | |
|---|---|
| **Hostname** | <server-host> |
| **IP** | <server-ip> |
| **OS** | Ubuntu 24.04.3 LTS |
| **CPU** | AMD EPYC-Milan, 16 cores |
| **RAM** | 64 GB |
| **Disk** | 338 GB SSD (`/dev/sda1`) |
| **Storage Box** | 10 TB SSHFS mount at `/mnt/storagebox` (Hetzner BX21) |
| **Provider** | Hetzner |

## Services

| Service | Domain | Stack Dir | Port |
|---------|--------|-----------|------|
| Twenty CRM | crm.<host-domain> | `stacks/twenty/` | 3003 |
| n8n | flow.<host-domain> | `stacks/n8n/` | 5678 |
| Dagster | dagster.<host-domain> | `stacks/dagster/` | 3010 |
| SurfSense | oracle.<host-domain> | `stacks/surfsense/` | 8000, 3001 |
| LangGraph | langgraph.<host-domain> | `stacks/langgraph/` | 8123 |
| Coder | coder.<host-domain> | systemd service | 3000 |
| Listmonk | (internal) | `stacks/listmonk/` | 9000 |
| Beszel | monitor.<host-domain> | `stacks/beszel/` | 8090 |
| Watchtower | — | `stacks/watchtower/` | — |

All services sit behind nginx with Let's Encrypt SSL. See `docs/architecture.md` for the full port map and topology.

## Quick Start

```bash
# SSH to server
ssh <admin-user>@<server-ip>

# Check service status
make status

# Backup all production databases
make backup-pg

# Backup stack configs
make backup-configs
```

## Repository Structure

```
devops-infra/
├── stacks/          # Docker Compose per service
├── nginx/           # Reverse proxy site configs
├── systemd/         # Custom systemd units (Storage Box mount, Coder)
├── scripts/         # Backup scripts (BorgBackup, R2)
├── docs/            # Architecture, runbook, research
├── Makefile         # Common operations
└── .env.example     # Backup credentials template
```

## Backups

Backups use **BorgBackup** to the Hetzner Storage Box (encrypted, deduplicated, compressed):

- **`backup_pg.py`** — dumps all 7 PostgreSQL databases
- **`backup_configs.py`** — archives all stack configs and secrets from `/opt`
- **`backup_foundry_datasets_cfr2.py`** — streams Foundry schema dump to Cloudflare R2

See `docs/runbook.md` for operational procedures.
