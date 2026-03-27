"""
Backup PostgreSQL databases to Hetzner Storage Box via BorgBackup.

Auto-discovers all running PostgreSQL containers via pg_isready, so new
databases are backed up without configuration changes. Manual overrides
can be specified in PROFILE_OVERRIDES for special cases (schema filters,
volume backups, dump_all mode). Borg repos are auto-initialized on first use.

Usage:
    backup_pg.py --production                      # backup all production databases (daily)
    backup_pg.py --all                             # backup everything (including non-production)
    backup_pg.py twenty-crm                        # backup a specific profile
    backup_pg.py --list twenty-crm                 # list archives
    backup_pg.py --verify n8n                      # verify repo integrity
    backup_pg.py --profiles                        # show discovered + configured profiles

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
    "ssh -i /home/<admin-user>/.ssh/id_ed25519"
    " -o StrictHostKeyChecking=accept-new"
    " -o ServerAliveInterval=60"
    " -o ServerAliveCountMax=3"
)
BACKUP_STATUS_DIR = Path(os.environ.get("BACKUP_STATUS_DIR", "/opt/backups/status"))

# Manual overrides for containers that need special backup configuration.
# Keyed by profile name; each must specify "container" to match a discovered
# PG container. Discovered containers without an override get default pg_dump
# behavior automatically.
PROFILE_OVERRIDES: dict[str, dict] = {
    "foundry-datasets": {
        "container": "foundry-datasets-db",
        "schema": "foundry",
        "production": False,
        "description": "Foundry datasets (foundry schema)",
    },
    "twenty-crm": {
        "container": "twenty-db",
        "exclude_schema": "foundry",
        "volumes": ["crm-stack_twenty-storage"],
        "description": "Twenty CRM (default db + file storage, excluding foundry schema)",
    },
    "dagster": {
        "container": "dagster-db",
        "dump_all": True,
        "description": "Dagster metadata + pipeline databases",
    },
    "langgraph": {
        "container": "llm-pipelines-postgres-1",
        "description": "LangGraph agent state",
    },
}

# Containers to skip during auto-discovery
SKIP_CONTAINERS: set[str] = set()

# Populated at runtime by build_profiles()
PROFILES: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


def discover_pg_containers() -> list[dict]:
    """Discover running PostgreSQL containers by probing with pg_isready."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("ERROR: Could not list Docker containers", file=sys.stderr)
        return []

    containers = []
    for name in sorted(result.stdout.strip().splitlines()):
        name = name.strip()
        if not name or name in SKIP_CONTAINERS:
            continue
        try:
            check = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-q"],
                capture_output=True, text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            continue
        if check.returncode != 0:
            continue

        # Read credentials from container environment
        pg_user = subprocess.run(
            ["docker", "exec", name, "printenv", "POSTGRES_USER"],
            capture_output=True, text=True,
        ).stdout.strip() or "postgres"
        pg_db = subprocess.run(
            ["docker", "exec", name, "printenv", "POSTGRES_DB"],
            capture_output=True, text=True,
        ).stdout.strip() or "postgres"

        containers.append({
            "container": name,
            "pg_user": pg_user,
            "database": pg_db,
        })
    return containers


def derive_profile_name(container_name: str) -> str:
    """Derive a human-readable profile name from a container name."""
    name = container_name
    for suffix in ("-db", "_db", "-postgres-1", "-postgres"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def build_profiles() -> dict[str, dict]:
    """Build profiles by merging auto-discovered containers with manual overrides."""
    discovered = discover_pg_containers()

    # Map container name -> (profile_name, override_config)
    override_by_container: dict[str, tuple[str, dict]] = {}
    for profile_name, override in PROFILE_OVERRIDES.items():
        override_by_container[override["container"]] = (profile_name, override)

    profiles: dict[str, dict] = {}

    for info in discovered:
        container = info["container"]

        if container in override_by_container:
            profile_name, override = override_by_container[container]
            # Discovered values are defaults; overrides win
            profile = {**info, **override}
        else:
            profile_name = derive_profile_name(container)
            profile = {
                **info,
                "description": f"{profile_name} PostgreSQL (auto-discovered)",
            }

        profile.setdefault("production", True)
        profiles[profile_name] = profile

    # Warn about overrides whose containers aren't running
    discovered_containers = {c["container"] for c in discovered}
    for profile_name, override in PROFILE_OVERRIDES.items():
        if override["container"] not in discovered_containers:
            print(
                f"Warning: container '{override['container']}' for profile "
                f"'{profile_name}' is not running — skipping",
                file=sys.stderr,
            )

    return profiles


def ensure_repo_initialized(profile_name: str) -> bool:
    """Check if borg repo exists; auto-initialize if not. Returns True if ready."""
    env = borg_env(profile_name)
    result = subprocess.run(
        ["borg", "list", "--short", "--last", "1"],
        capture_output=True, text=True,
        env=env,
    )
    if result.returncode == 0:
        return True

    # Repo doesn't exist — initialize it
    repo = borg_repo(profile_name)
    print(f"Auto-initializing new borg repo: {repo}")
    init_result = subprocess.run(
        ["borg", "init", "--encryption=repokey"],
        env=env,
    )
    if init_result.returncode == 0:
        print(f"Repository initialized: {repo}")
        return True

    print(f"ERROR: Could not initialize borg repo for {profile_name}", file=sys.stderr)
    return False


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
    if not ensure_repo_initialized(profile_name):
        return False

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
    has_override = {o["container"] for o in PROFILE_OVERRIDES.values()}
    print(f"{'Profile':<22} {'Prod':<6} {'Source':<14} {'Container':<34} Description")
    print("-" * 115)
    for name, p in PROFILES.items():
        prod = "yes" if p.get("production", True) else "no"
        source = "override" if p["container"] in has_override else "discovered"
        print(f"{name:<22} {prod:<6} {source:<14} {p['container']:<34} {p.get('description', '')}")


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

    # Discover running PG containers and merge with overrides
    global PROFILES
    PROFILES = build_profiles()
    print(f"Discovered {len(PROFILES)} PostgreSQL database(s)\n")

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
