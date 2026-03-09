"""
Backup PostgreSQL databases to Hetzner Storage Box via BorgBackup.

Each database profile gets its own borg repository on the storage box. Borg handles
compression, encryption, deduplication, and transport. pg_dump runs inside Docker
via --content-from-command — if pg_dump fails, no archive is created.

Usage:
    backup_pg.py --production                      # backup all production databases (daily)
    backup_pg.py --all                             # backup everything (including foundry-datasets)
    backup_pg.py foundry-datasets                  # backup a specific profile
    backup_pg.py --list foundry-datasets           # list archives
    backup_pg.py --verify n8n                      # verify repo integrity
    backup_pg.py --init n8n                        # initialize new borg repo
    backup_pg.py --profiles                        # show available profiles

Environment variables (from .env):
    BORG_PASSPHRASE  — encryption passphrase (shared across all repos)
    BORG_BASE        — storage box base URL (default: ssh://<storage-box-id>@<storage-box-id>.your-storagebox.de:23/.)

PG passwords are read from each Docker container's POSTGRES_PASSWORD env var.

Requires on the host: borg, docker, nice, ionice
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BORG_PASSPHRASE = os.environ["BORG_PASSPHRASE"]
BORG_BASE = os.environ.get(
    "BORG_BASE",
    "ssh://<storage-box-id>@<storage-box-id>.your-storagebox.de:23/.",
)
BORG_RSH = (
    "ssh -o StrictHostKeyChecking=accept-new"
    " -o ServerAliveInterval=60"
    " -o ServerAliveCountMax=3"
)
BACKUP_STATUS_DIR = Path(os.environ.get("BACKUP_STATUS_DIR", "/opt/backup-status"))

PROFILES: dict[str, dict] = {
    "foundry-datasets": {
        "container": "foundry-datasets-db",
        "pg_user": "twenty",
        "database": "default",
        "schema": "foundry",
        "production": False,
        "description": "Foundry datasets (foundry schema)",
    },
    "twenty-crm": {
        "container": "twenty-db",
        "pg_user": "twenty",
        "database": "default",
        "exclude_schema": "foundry",
        "volumes": ["crm-stack_twenty-storage"],
        "description": "Twenty CRM (default db + file storage, excluding foundry schema)",
    },
    "n8n": {
        "container": "n8n-db",
        "pg_user": "n8n",
        "database": "n8n",
        "description": "n8n workflows and execution history",
    },
    "surfsense": {
        "container": "surfsense-db",
        "pg_user": "postgres",
        "database": "surfsense",
        "description": "SurfSense + pgvector embeddings",
    },
    "dagster": {
        "container": "dagster-db",
        "pg_user": "dagster",
        "dump_all": True,
        "description": "Dagster metadata + pipeline databases",
    },
    "langgraph": {
        "container": "langgraph-db",
        "pg_user": "postgres",
        "database": "langgraph",
        "description": "LangGraph agent state",
    },
    "listmonk": {
        "container": "listmonk_db",
        "pg_user": "listmonk",
        "database": "listmonk",
        "description": "Listmonk mailing list manager",
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_status(
    profile_name: str,
    archive_name: str | None,
    success: bool,
    error_msg: str | None = None,
) -> None:
    """Write a JSON status file for Dagster observability."""
    profile = PROFILES[profile_name]
    now = datetime.now(timezone.utc)
    status: dict = {
        "profile": profile_name,
        "type": "pg",
        "status": "ok" if success else "error",
        "archive": archive_name,
        "timestamp": now.isoformat(),
        "description": profile.get("description", ""),
    }
    if error_msg:
        status["error"] = error_msg

    BACKUP_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    status_file = BACKUP_STATUS_DIR / f"pg-{profile_name}.json"
    status_file.write_text(json.dumps(status, indent=2) + "\n")
    print(f"Status written to {status_file}")


def borg_repo(profile_name: str) -> str:
    return f"{BORG_BASE}/{profile_name}"


def borg_env(profile_name: str) -> dict[str, str]:
    env = os.environ.copy()
    env["BORG_REPO"] = borg_repo(profile_name)
    env["BORG_PASSPHRASE"] = BORG_PASSPHRASE
    env["BORG_RSH"] = BORG_RSH
    return env


def get_pg_password(container: str) -> str:
    """Read POSTGRES_PASSWORD from a Docker container's environment."""
    result = subprocess.run(
        ["docker", "exec", container, "printenv", "POSTGRES_PASSWORD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Warning: could not read POSTGRES_PASSWORD from {container}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def build_dump_cmd(profile: dict) -> list[str]:
    """Build the pg_dump or pg_dumpall command for a profile."""
    container = profile["container"]
    pg_user = profile["pg_user"]
    pg_password = get_pg_password(container)

    base = ["docker", "exec"]
    if pg_password:
        base.extend(["-e", f"PGPASSWORD={pg_password}"])
    base.append(container)

    if profile.get("dump_all"):
        return [*base, "pg_dumpall", "-h", "localhost", "-p", "5432", "-U", pg_user]

    cmd = [
        *base,
        "pg_dump",
        "--host=localhost", "--port=5432",
        f"--username={pg_user}",
        f"--dbname={profile['database']}",
    ]
    if "schema" in profile:
        cmd.append(f"--schema={profile['schema']}")
    if "exclude_schema" in profile:
        cmd.append(f"--exclude-schema={profile['exclude_schema']}")
    return cmd


def resolve_volume_path(volume_name: str) -> str | None:
    """Resolve a Docker volume name to its host mountpoint."""
    result = subprocess.run(
        ["docker", "volume", "inspect", volume_name, "--format", "{{.Mountpoint}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Warning: could not resolve volume {volume_name}", file=sys.stderr)
        return None
    return result.stdout.strip()


def backup_volumes(profile_name: str, volumes: list[str], timestamp: str) -> bool:
    """Backup Docker volumes as a file archive in the same borg repo."""
    paths = []
    for vol in volumes:
        path = resolve_volume_path(vol)
        if path:
            paths.append(path)

    if not paths:
        print(f"Warning: no volume paths resolved for {profile_name}", file=sys.stderr)
        return True  # not a fatal error

    archive_name = f"{profile_name}-storage-{timestamp}"
    repo = borg_repo(profile_name)

    cmd = [
        "nice", "-n", "19",
        "ionice", "-c", "3",
        "borg", "create",
        "--compression", "zstd,3",
        "--upload-ratelimit", "10000",
        "-v", "--stats", "--show-rc",
        f"::{archive_name}",
        *paths,
    ]

    print(f"\nBacking up volumes -> {repo}::{archive_name}")
    for p in paths:
        print(f"  {p}")
    print()

    result = subprocess.run(cmd, env=borg_env(profile_name))

    if result.returncode != 0:
        print(f"\nVolume backup FAILED for {profile_name} (exit code {result.returncode})", file=sys.stderr)
        return False

    print(f"\nVolume backup complete: {repo}::{archive_name}")
    return True


def run_backup(profile_name: str) -> bool:
    """Run borg create for a profile. Returns True on success."""
    profile = PROFILES[profile_name]
    now = datetime.now(timezone.utc)
    timestamp = f"{now:%Y-%m-%d_%H%M%S}"
    archive_name = f"{profile_name}-{timestamp}"

    dump_cmd = build_dump_cmd(profile)

    cmd = [
        "nice", "-n", "19",
        "ionice", "-c", "3",
        "borg", "create",
        "--content-from-command",
        "--compression", "zstd,3",
        "--upload-ratelimit", "10000",
        "-v", "--stats", "--show-rc", "--progress",
        f"::{archive_name}",
        "--",
        *dump_cmd,
    ]

    repo = borg_repo(profile_name)
    print(f"Backing up {profile_name} -> {repo}::{archive_name}")
    print(f"  {profile.get('description', '')}")
    print(f"  Container: {profile['container']}")
    print(f"  Compression: zstd,3 | Upload limit: 10 MB/s | Priority: idle")
    print()

    result = subprocess.run(cmd, env=borg_env(profile_name))

    if result.returncode != 0:
        print(f"\nBackup FAILED for {profile_name} (exit code {result.returncode})", file=sys.stderr)
        return False

    print(f"\nBackup complete: {repo}::{archive_name}")

    # Backup associated Docker volumes if configured
    if "volumes" in profile:
        if not backup_volumes(profile_name, profile["volumes"], timestamp):
            return False

    return True


def init_repo(profile_name: str) -> None:
    """Initialize a new borg repository."""
    repo = borg_repo(profile_name)
    print(f"Initializing {repo} ...")
    result = subprocess.run(
        ["borg", "init", "--encryption=repokey"],
        env=borg_env(profile_name),
    )
    if result.returncode == 0:
        print(f"Repository initialized: {repo}")
    sys.exit(result.returncode)


def list_archives(profile_name: str) -> None:
    result = subprocess.run(["borg", "list"], env=borg_env(profile_name))
    sys.exit(result.returncode)


def verify_repo(profile_name: str) -> None:
    repo = borg_repo(profile_name)
    print(f"Verifying {repo} ...")
    result = subprocess.run(["borg", "check", "-v", "--show-rc"], env=borg_env(profile_name))
    sys.exit(result.returncode)


def show_profiles() -> None:
    print(f"{'Profile':<22} {'Prod':<6} {'Container':<38} Description")
    print("-" * 105)
    for name, p in PROFILES.items():
        prod = "yes" if p.get("production", True) else "no"
        print(f"{name:<22} {prod:<6} {p['container']:<38} {p.get('description', '')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Backup PostgreSQL databases to Hetzner Storage Box via BorgBackup"
    )
    parser.add_argument("profile", nargs="?", help="Profile name to backup")
    parser.add_argument("--production", action="store_true", help="Backup all production profiles")
    parser.add_argument("--all", action="store_true", help="Backup all profiles (including non-production)")
    parser.add_argument("--list", action="store_true", help="List archives for a profile")
    parser.add_argument("--verify", action="store_true", help="Verify repository integrity")
    parser.add_argument("--init", action="store_true", help="Initialize a new borg repository")
    parser.add_argument("--profiles", action="store_true", help="Show available profiles")
    args = parser.parse_args()

    if args.profiles:
        show_profiles()
        return

    if args.production or args.all:
        failed = []
        for name, profile in PROFILES.items():
            if not args.all and not profile.get("production", True):
                print(f"Skipping {name} (non-production)\n")
                continue
            now = datetime.now(timezone.utc)
            archive_name = f"{name}-{now:%Y-%m-%d_%H%M%S}"
            success = run_backup(name)
            if success:
                write_status(name, archive_name, success=True)
            else:
                write_status(name, archive_name, success=False, error_msg=f"backup failed for {name}")
                failed.append(name)
            print()
        if failed:
            print(f"\nFailed profiles: {', '.join(failed)}", file=sys.stderr)
            sys.exit(1)
        label = "All" if args.all else "Production"
        print(f"\n{label} backups complete.")
        return

    if not args.profile:
        parser.print_help()
        print(f"\nAvailable profiles: {', '.join(PROFILES)}")
        sys.exit(1)

    if args.profile not in PROFILES:
        print(f"Unknown profile: {args.profile}", file=sys.stderr)
        print(f"Available: {', '.join(PROFILES)}", file=sys.stderr)
        sys.exit(1)

    if args.init:
        init_repo(args.profile)
        return

    if args.list:
        list_archives(args.profile)
        return

    if args.verify:
        verify_repo(args.profile)
        return

    now = datetime.now(timezone.utc)
    archive_name = f"{args.profile}-{now:%Y-%m-%d_%H%M%S}"
    success = run_backup(args.profile)
    write_status(args.profile, archive_name, success=success,
                 error_msg=f"backup failed for {args.profile}" if not success else None)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
