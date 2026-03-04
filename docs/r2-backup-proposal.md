# Proposal: Cloudflare R2 for Mediasets & Database Backup

## Problem

We're extracting all data from Palantir Foundry onto a single server (<server-ip>). Two gaps:

1. **Mediasets** (Zoom audio/video recordings) are binary files that can't go into PostgreSQL. They need S3-compatible object storage for programmatic access from Dagster/n8n pipelines.
2. **No database backup.** The PostgreSQL database (currently 40 GB, growing to ~150-250 GB once all sectors are extracted) lives on a single disk with no replication. If the server dies, all extracted data is lost.

## Proposal

Use **Cloudflare R2** as a single storage layer for both:
- **Mediasets**: Zoom audio + video files, accessed programmatically via S3 API from pipelines
- **Database backups**: Scheduled `pg_dump` compressed backups, uploaded daily

## Why Cloudflare R2?

- **Zero egress fees** — reading data back out is free (AWS S3 charges $0.09/GB)
- **S3-compatible API** — works with `boto3`, Dagster, n8n, any S3 tool. No code changes vs AWS S3
- **We already use Cloudflare** — `*.<host-domain>` is behind Cloudflare Access
- **No infrastructure to manage** — unlike self-hosted MinIO, there's nothing to maintain
- **Built-in redundancy** — data is replicated across Cloudflare's network

## Pricing (verified Feb 2026)

| Item | Cost |
|------|------|
| Storage | $0.015 / GB / month |
| Writes (Class A ops) | $4.50 / million requests |
| Reads (Class B ops) | $0.36 / million requests |
| Egress (download) | **Free** |
| Free tier | 10 GB storage, 1M writes, 10M reads / month |

Source: https://developers.cloudflare.com/r2/pricing/

## Cost Estimates

### Storage costs by volume

| Total stored | Monthly cost | Annual cost |
|-------------|-------------|-------------|
| 50 GB | $0.75 | $9 |
| 100 GB | $1.50 | $18 |
| 250 GB | $3.75 | $45 |
| 500 GB | $7.50 | $90 |
| 1 TB | $15.00 | $180 |
| 5 TB | $75.00 | $900 |

### Our estimated usage

| Component | Estimated size | Notes |
|-----------|---------------|-------|
| PG backup (compressed) | ~30-60 GB | `pg_dump --compress` typically 3-5x compression on text-heavy data |
| Zoom mediasets | TBD | 2 mediasets (S3 Audio + S3 Video) — size not yet measured |
| Future sector mediasets | TBD | Other sectors may have mediasets too |

**Without mediasets** (DB backup only): ~.50-1.00/month
**With 500 GB of mediasets** : ~8-9/month
**With 2 TB of mediasets**: ~32/month

### Comparison: same storage on AWS S3

| Scenario | Cloudflare R2 | AWS S3 Standard |
|----------|--------------|-----------------|
| 250 GB stored | $3.75/mo | $5.75/mo + egress |
| 250 GB stored + 250 GB read/month | $3.75/mo | $28.25/mo |
| 1 TB stored + 500 GB read/month | $15.00/mo | $68.00/mo |

The difference is entirely due to R2's zero egress fees. For pipelines that frequently read mediasets, this adds up quickly.

### Operations costs (negligible)

A daily pg_dump upload = ~30 Class A operations/month. Mediaset pipeline reads might add a few thousand Class B ops/month. At $4.50 and $0.36 per million respectively, operations cost is effectively $0.

## What this looks like in practice

### Database backup
- A Dagster job (or cron) runs `pg_dump` daily, compresses the output, uploads to R2
- Bucket: `devops-backups/pg/daily/2026-02-27.sql.gz`
- Retention: keep last 30 days, delete older dumps automatically (R2 lifecycle rules)
- Recovery: download dump, `pg_restore` to a new PG instance

### Mediaset storage
- Foundry mediasets downloaded and stored in R2: `devops-mediasets/zoom/audio/...`, `devops-mediasets/zoom/video/...`
- Dagster/n8n pipelines access via S3 API (`boto3`)
- Any future service can read the same files using standard S3 SDKs

### Access from code
```python
import boto3

s3 = boto3.client("s3",
    endpoint_url="https://<account_id>.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
)

# Upload a backup
s3.upload_file("dump.sql.gz", "devops-backups", "pg/daily/2026-02-27.sql.gz")

# Read a mediaset file in a pipeline
s3.download_file("devops-mediasets", "zoom/audio/meeting-123.mp3", "/tmp/meeting.mp3")
```

## Setup effort

- Create R2 bucket in Cloudflare dashboard (~5 min)
- Generate API tokens (~5 min)
- Add credentials to server `.env` files
- Write a pg_dump backup script/Dagster job (~1 hour)
- Write mediaset download script (~2-4 hours, depending on Foundry mediaset API)

## Recommendation

Start with the DB backup (low effort, immediate value). Add mediasets once we've measured their size in Foundry and confirmed the download approach.
