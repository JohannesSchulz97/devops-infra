# Research Prompt: Hetzner Server Backups vs Cloudflare R2 for PostgreSQL Backup Strategy

## Context

We run a Hetzner dedicated server (226 GB disk, currently 90% full) hosting multiple services via Docker Compose:

- **PostgreSQL 16** (port 5435) — runs inside a Docker container as part of a Twenty CRM stack. The `foundry` schema contains ~130 GB of data extracted from Palantir Foundry (our data platform that we're migrating away from). This is a one-time bulk extraction — the data is written once and then mostly read by downstream pipelines.
- **Dagster** (pipeline orchestrator) — reads from the PG database and an external MariaDB, writes pipeline outputs back to PG
- **n8n** (workflow automation)
- **SurfSense**, **LangGraph**, and other services each with their own PG instances on different ports

The PostgreSQL instance on port 5435 is the critical one — it holds all our Foundry backup data plus the Twenty CRM data. There is currently **no backup strategy** in place. If the server dies, all extracted data is lost (and re-extraction from Foundry may not be possible in the future as we're migrating away).

## What We're Evaluating

### Option A: Hetzner Built-in Backups
Hetzner offers server backups at 20% of the monthly server cost. Their description:
> "Jeder Server hat sieben Slots für Backups. Wenn alle Slots voll sind, ersetzt das neueste Backup das älteste."
> (Each server has seven backup slots. When all slots are full, the newest backup replaces the oldest.)

### Option B: pg_dump to Cloudflare R2
Scheduled `pg_dump` (compressed) uploaded to Cloudflare R2 object storage ($0.015/GB/month, zero egress). We'd write a Dagster job or cron script to do this daily.

### Option C: Both
Use Hetzner backups for full server disaster recovery (OS, Docker configs, all services) and pg_dump to R2 for granular, guaranteed-consistent PostgreSQL backups.

## Questions to Research

### Hetzner Backups
1. **Consistency**: Does Hetzner do live disk snapshots while the server is running? If so, is the snapshot crash-consistent? Will PostgreSQL (running in Docker) recover cleanly from a snapshot taken mid-write?
2. **Scheduling**: Can you control when backups run? Or does Hetzner decide? Can you trigger a manual backup before a risky operation?
3. **Frequency**: How often do automatic backups run? Daily? Weekly? Is it configurable?
4. **Restore process**: How do you restore? Full server only, or can you mount the backup and extract individual files? How long does restore take for a ~200 GB disk?
5. **Retention**: With 7 slots and automatic rotation, what's the effective retention period? (e.g., if daily = 7 days, if weekly = 7 weeks)
6. **Scope**: Does it back up the entire disk including Docker volumes? Our PG data lives in a Docker named volume (`crm-stack_twenty-db-data`).
7. **Reliability**: Are Hetzner backups stored in the same datacenter? Different datacenter? What happens if the datacenter has an issue?
8. **Cost**: For a dedicated server (not cloud), does the 20% pricing still apply? Or is it different for dedicated vs cloud servers?
9. **Limitations**: Any size limits? Performance impact during backup? Network bandwidth consumed?

### PostgreSQL-Specific Concerns
10. **Crash consistency for PG in Docker**: If Hetzner snapshots the disk while PG is actively writing WAL and data files, will PG's crash recovery (WAL replay) reliably recover the database? Are there known edge cases where this fails?
11. **Best practice**: What do PostgreSQL experts recommend for backing up PG running in Docker on Hetzner? Is there a way to hook into Hetzner's backup process (pre/post scripts) to run `pg_start_backup` / `pg_stop_backup`?
12. **pg_dump size estimate**: For a 130 GB database that's mostly TEXT columns, what compression ratio can we expect with `pg_dump --compress=gzip`? (This affects R2 storage costs.)

### Comparison
13. **Recovery time**: Hetzner full restore vs spinning up a new PG and loading a pg_dump — which is faster for a ~130 GB database?
14. **Granularity**: Can Hetzner backups restore individual files/volumes, or is it all-or-nothing?
15. **Cost comparison**: Hetzner backup (20% of server cost) vs R2 storage for compressed pg_dumps (at $0.015/GB/month with 30-day retention)

## Our Priorities
1. **Data safety**: The Foundry extraction data is irreplaceable. This is the #1 priority.
2. **Low maintenance**: We're a small team. Prefer set-and-forget solutions.
3. **Cost efficiency**: We're cost-conscious but willing to pay for reliability.
4. **Recovery speed**: Nice to have but not critical — hours of downtime is acceptable, days is not.

## Ideal Output
A recommendation on which option (A, B, or C) best fits our use case, with specific configuration advice for the chosen approach. If Option C, clarify what each layer is responsible for.
