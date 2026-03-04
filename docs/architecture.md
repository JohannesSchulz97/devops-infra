# Architecture

## Server: <server-host> (<server-ip>)

### Hardware & OS

- **Provider**: Hetzner Dedicated Server
- **OS**: Ubuntu 24.04.3 LTS (kernel 6.8.0)
- **CPU**: AMD EPYC-Milan, 16 cores
- **RAM**: 64 GB
- **Disk**: 338 GB SSD (`/dev/sda1`)
- **External Storage**: Hetzner Storage Box BX21, 10 TB, mounted via SSHFS at `/mnt/storagebox`

### Network Topology

```
Internet
  │
  ├── :80  ──→ nginx (HTTP → HTTPS redirect)
  ├── :443 ──→ nginx (SSL termination, Let's Encrypt)
  │             ├── crm.<host-domain>            → 127.0.0.1:3003  (Twenty CRM)
  │             ├── flow.<host-domain>           → 127.0.0.1:5678  (n8n)
  │             ├── dagster.<host-domain>        → 127.0.0.1:3010  (Dagster)
  │             ├── oracle.<host-domain>         → 127.0.0.1:8000/3001 (SurfSense)
  │             ├── langgraph.<host-domain>      → 127.0.0.1:8123  (LangGraph)
  │             ├── coder.<host-domain>          → 127.0.0.1:3000  (Coder)
  │             ├── vibekanban.<host-domain>     → 127.0.0.1:8082  (VibeKanban)
  │             └── api.vibekanban.<host-domain> → 127.0.0.1:8081  (VibeKanban API)
  │
  └── (no other ports exposed to internet)
```

All application ports are bound to 127.0.0.1 except n8n (5678) and SurfSense backend (8000) which are on 0.0.0.0. These should ideally be locked down to localhost as well.

### Port Map

| Port | Binding | Service | Protocol |
|------|---------|---------|----------|
| 80 | 0.0.0.0 | nginx | HTTP (redirect) |
| 443 | 0.0.0.0 | nginx | HTTPS |
| 3000 | 127.0.0.1 | Coder | HTTP |
| 3001 | 0.0.0.0 | SurfSense frontend | HTTP |
| 3003 | 0.0.0.0 | Twenty CRM server | HTTP |
| 3010 | 0.0.0.0 | Dagster webserver | HTTP |
| 5050 | 127.0.0.1 | pgAdmin (SurfSense) | HTTP |
| 5433 | 127.0.0.1 | SurfSense PostgreSQL | PostgreSQL |
| 5434 | 127.0.0.1 | LangGraph PostgreSQL | PostgreSQL |
| 5435 | 127.0.0.1 | Foundry datasets PostgreSQL | PostgreSQL |
| 5436 | 127.0.0.1 | Dagster PostgreSQL | PostgreSQL |
| 5437 | 127.0.0.1 | Twenty CRM PostgreSQL | PostgreSQL |
| 5678 | 0.0.0.0 | n8n | HTTP |
| 6379 | 127.0.0.1 | SurfSense Redis | Redis |
| 6380 | 127.0.0.1 | LangGraph Redis | Redis |
| 8000 | 0.0.0.0 | SurfSense backend | HTTP |
| 8123 | 127.0.0.1 | LangGraph API | HTTP |
| 9000 | 127.0.0.1 | Listmonk | HTTP |

### PostgreSQL Instances

Seven separate PostgreSQL containers, each with its own data directory:

| Container | Port | User | Database(s) | Data Volume |
|-----------|------|------|-------------|-------------|
| foundry-datasets-db | 5435 | twenty | default (foundry schema) | /mnt/main/pg_main |
| twenty-db | 5437 | twenty | default | /mnt/main/pg_twenty |
| dagster-db | 5436 | dagster | dagster, pipelines, pipelines_dev | /mnt/main/pg_dagster |
| n8n-db | (internal) | n8n | n8n | /mnt/main/pg_n8n |
| surfsense-db | 5433 | postgres | surfsense | /mnt/main/pg_surfsense |
| langgraph-db | 5434 | postgres | langgraph | pgdata named volume |
| listmonk_db | (internal) | listmonk | listmonk | (managed by listmonk stack) |

### Disk Layout

```
/dev/sda1 (338 GB SSD)
├── /                        # OS, Docker, application code
├── /opt/                    # Docker Compose stacks (live configs + .env files)
│   ├── twenty/
│   ├── n8n/
│   ├── dagster/
│   ├── SurfSense/
│   ├── listmonk/
│   ├── llm-pipelines/   # LangGraph
│   ├── watchtower/
│   ├── vibe-kanban/
│   └── foundry-backup/  # Foundry extraction scripts (deployed subset)
├── /mnt/main/               # PostgreSQL data directories
│   ├── pg_main/             # foundry-datasets-db
│   ├── pg_twenty/           # twenty-db
│   ├── pg_dagster/          # dagster-db
│   ├── pg_n8n/              # n8n-db
│   └── pg_surfsense/        # surfsense-db
└── /var/lib/docker/         # Docker volumes, images, containers

/mnt/storagebox (10 TB SSHFS → Hetzner Storage Box BX21)
└── foundry-mediasets/       # Extracted Foundry binary files (~266 GB)
    ├── <sector>/
    │   └── <mediaset-name>/
    └── global/
```

### SSL Certificates

All certificates managed by Certbot (Let's Encrypt). Domains:

- crm.<host-domain>
- flow.<host-domain>
- dagster.<host-domain>
- oracle.<host-domain>
- langgraph.<host-domain>
- coder.<host-domain>, *.coder.<host-domain> (wildcard)
- vibekanban.<host-domain>
- api.vibekanban.<host-domain>

Auto-renewal via Certbot timer: `systemctl status certbot.timer`

### Backup Architecture

```
Server (<server-host>)
  │
  ├── BorgBackup → Storage Box (ssh://<storage-box-id>@...your-storagebox.de:23)
  │   ├── server-configs/     # /opt stack configs (backup_configs.py)
  │   ├── foundry-datasets/   # pg_dump of foundry schema
  │   ├── twenty-crm/         # pg_dump of Twenty + file storage volume
  │   ├── n8n/                # pg_dump of n8n
  │   ├── surfsense/          # pg_dump of surfsense
  │   ├── dagster/            # pg_dumpall of dagster
  │   ├── langgraph/          # pg_dump of langgraph
  │   └── listmonk/           # pg_dump of listmonk
  │
  └── pg_dump → Cloudflare R2 (devops-backup bucket)
      └── foundry-datasets/   # Compressed foundry schema (off-site copy)
```

### Service Dependencies

```
Dagster → MariaDB (external, via SSH tunnel from dev machines)
        → PostgreSQL (dagster-db, local)
        → Twenty CRM PostgreSQL (twenty-db, local)

Twenty CRM → PostgreSQL (twenty-db, local)
           → Redis (local)

n8n → PostgreSQL (n8n-db, local)

SurfSense → PostgreSQL + pgvector (surfsense-db, local)
          → Redis (local)

LangGraph → PostgreSQL (langgraph-db, local)
          → Redis (local)

Listmonk → PostgreSQL (foundry-datasets-db on 5435, external)
         → Mailgun SMTP

Coder → PostgreSQL (localhost:5432, separate instance)
      → Docker socket

Watchtower → Docker socket (monitors labeled containers)
```
