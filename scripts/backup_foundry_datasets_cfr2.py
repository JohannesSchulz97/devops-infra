"""
Backup the Foundry datasets (foundry schema) from PostgreSQL to Cloudflare R2.

Streams pg_dump | gzip directly to R2 via S3 multipart upload — no temp file
on disk. Suitable for large databases on servers with limited free disk space.

Usage:
    uv run python scripts/backup_foundry_datasets_cfr2.py
    uv run python scripts/backup_foundry_datasets_cfr2.py --list    # list existing backups in R2

Environment variables (from .env):
    PG_USER, PG_PASSWORD, PG_DATABASE, PG_CONTAINER
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import boto3

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PG_CONTAINER = os.environ.get("PG_CONTAINER", "crm-stack-db-1")
PG_USER = os.environ.get("PG_USER", "twenty")
PG_PASSWORD = os.environ["PG_PASSWORD"]
PG_DATABASE = os.environ.get("PG_DATABASE", "default")
PG_SCHEMA = os.environ.get("PG_SCHEMA", "foundry")

R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ.get("R2_BUCKET", "devops-backup")

R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
R2_PREFIX = "foundry-datasets"

# S3 multipart: minimum part size is 5 MB, we use 64 MB for fewer requests
PART_SIZE = 64 * 1024 * 1024
READ_SIZE = 1024 * 1024  # 1 MB reads from pg_dump stdout

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def stream_pg_dump_to_r2(r2_key: str) -> None:
    """Stream pg_dump | gzip directly to R2 via multipart upload."""
    env = os.environ.copy()
    env["PGPASSWORD"] = PG_PASSWORD

    # pg_dump runs inside the Docker container, piped through gzip on the host
    shell_cmd = (
        f"docker exec -e PGPASSWORD={PG_PASSWORD} {PG_CONTAINER} "
        f"pg_dump --host=localhost --port=5432 --username={PG_USER} "
        f"--dbname={PG_DATABASE} --schema={PG_SCHEMA} "
        f"| gzip -6"
    )

    s3 = get_r2_client()

    print(f"Starting pg_dump for schema '{PG_SCHEMA}' -> r2://{R2_BUCKET}/{r2_key}")
    start = time.time()

    proc = subprocess.Popen(
        shell_cmd, shell=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    mpu = s3.create_multipart_upload(
        Bucket=R2_BUCKET,
        Key=r2_key,
        StorageClass="STANDARD_IA",
    )
    upload_id = mpu["UploadId"]

    parts = []
    part_number = 1
    total_compressed = 0
    buf = io.BytesIO()

    try:
        while True:
            chunk = proc.stdout.read(READ_SIZE)
            if not chunk:
                break

            buf.write(chunk)

            if buf.tell() >= PART_SIZE:
                part_data = buf.getvalue()
                resp = s3.upload_part(
                    Bucket=R2_BUCKET,
                    Key=r2_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=part_data,
                )
                parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
                total_compressed += len(part_data)
                print(
                    f"  Part {part_number}: {len(part_data) / (1024*1024):.1f} MB "
                    f"(total: {total_compressed / (1024*1024*1024):.2f} GB)"
                )
                part_number += 1
                buf = io.BytesIO()

        # Upload remaining data
        remaining = buf.getvalue()
        if remaining:
            resp = s3.upload_part(
                Bucket=R2_BUCKET,
                Key=r2_key,
                UploadId=upload_id,
                PartNumber=part_number,
                Body=remaining,
            )
            parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
            total_compressed += len(remaining)
            print(f"  Part {part_number} (final): {len(remaining) / (1024*1024):.1f} MB")

        proc.wait()
        if proc.returncode != 0:
            stderr = proc.stderr.read().decode()
            raise RuntimeError(f"pg_dump | gzip failed (exit {proc.returncode}): {stderr}")

        s3.complete_multipart_upload(
            Bucket=R2_BUCKET,
            Key=r2_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

        elapsed = time.time() - start
        total_gb = total_compressed / (1024 * 1024 * 1024)
        print(f"\nBackup complete: r2://{R2_BUCKET}/{r2_key}")
        print(f"  Size: {total_gb:.2f} GB compressed")
        print(f"  Time: {elapsed / 60:.1f} minutes")

    except Exception:
        print("Error — aborting multipart upload...", file=sys.stderr)
        s3.abort_multipart_upload(Bucket=R2_BUCKET, Key=r2_key, UploadId=upload_id)
        proc.kill()
        raise


def list_backups() -> None:
    """List existing backups in R2."""
    s3 = get_r2_client()
    response = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=R2_PREFIX)

    contents = response.get("Contents", [])
    if not contents:
        print("No backups found.")
        return

    print(f"{'Date':<14} {'Size':>10}  Key")
    print("-" * 60)
    for obj in sorted(contents, key=lambda o: o["Key"]):
        size_mb = obj["Size"] / (1024 * 1024)
        modified = obj["LastModified"].strftime("%Y-%m-%d")
        print(f"{modified:<14} {size_mb:>8.1f} MB  {obj['Key']}")

    total_gb = sum(o["Size"] for o in contents) / (1024 * 1024 * 1024)
    print(f"\n{len(contents)} backups, {total_gb:.2f} GB total")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Backup Foundry datasets (foundry schema) from PostgreSQL to R2"
    )
    parser.add_argument("--list", action="store_true", help="List existing backups in R2")
    args = parser.parse_args()

    if args.list:
        list_backups()
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r2_key = f"{R2_PREFIX}/{today}.sql.gz"

    stream_pg_dump_to_r2(r2_key)


if __name__ == "__main__":
    main()
