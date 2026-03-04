"""
Backup all stack configs and secrets from /opt to Hetzner Storage Box via BorgBackup.

Collects docker-compose files, .env files, and deploy directories from all stacks
in /opt, tars them, and sends to a borg repo. Runs as root to access all files.

Usage:
    backup_configs.py                  # backup all configs
    backup_configs.py --list           # list archives
    backup_configs.py --verify         # verify repo integrity
    backup_configs.py --init           # initialize borg repo

Environment variables (from .env):
    BORG_PASSPHRASE  — encryption passphrase
    BORG_BASE        — storage box base URL

Requires on the host: borg, sudo, tar
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

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

REPO_NAME = "server-configs"
STACKS_DIR = "/opt"

# Files/dirs to include from each stack
INCLUDE_PATTERNS = [
    "docker-compose.yml",
    "docker-compose.yaml",
    "docker-compose.*.yml",
    "docker-compose.*.yaml",
    ".env",
    "*.yaml",
    "*.yml",
    "deploy/",
    "Caddyfile",
    "nginx.conf",
    "Dockerfile",
]

# Stacks to skip (not actual service stacks)
SKIP_DIRS = {"containerd", "foundry-backup"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def borg_repo() -> str:
    return f"{BORG_BASE}/{REPO_NAME}"


def borg_env() -> dict[str, str]:
    env = os.environ.copy()
    env["BORG_REPO"] = borg_repo()
    env["BORG_PASSPHRASE"] = BORG_PASSPHRASE
    env["BORG_RSH"] = BORG_RSH
    return env


def collect_config_files() -> list[str]:
    """Find all config files across /opt stacks."""
    import glob

    files = []
    for entry in sorted(os.listdir(STACKS_DIR)):
        stack_dir = os.path.join(STACKS_DIR, entry)
        if not os.path.isdir(stack_dir) or entry.startswith(".") or entry in SKIP_DIRS:
            continue

        for pattern in INCLUDE_PATTERNS:
            full_pattern = os.path.join(stack_dir, pattern)
            for match in glob.glob(full_pattern):
                if os.path.isfile(match) or os.path.isdir(match):
                    files.append(match)

    return sorted(set(files))


def run_backup() -> None:
    """Create a borg archive of all stack config files."""
    now = datetime.now(timezone.utc)
    archive_name = f"configs-{now:%Y-%m-%d_%H%M%S}"

    files = collect_config_files()
    if not files:
        print("No config files found to backup.", file=sys.stderr)
        sys.exit(1)

    print(f"Backing up {len(files)} config files -> {borg_repo()}::{archive_name}")
    for f in files:
        print(f"  {f}")
    print()

    cmd = [
        "borg", "create",
        "--compression", "zstd,3",
        "-v", "--stats", "--show-rc",
        f"::{archive_name}",
        *files,
    ]

    result = subprocess.run(cmd, env=borg_env())

    if result.returncode != 0:
        print(f"\nBackup FAILED (exit code {result.returncode})", file=sys.stderr)
        sys.exit(result.returncode)

    print(f"\nBackup complete: {borg_repo()}::{archive_name}")


def init_repo() -> None:
    repo = borg_repo()
    print(f"Initializing {repo} ...")
    result = subprocess.run(
        ["borg", "init", "--encryption=repokey"],
        env=borg_env(),
    )
    if result.returncode == 0:
        print(f"Repository initialized: {repo}")
    sys.exit(result.returncode)


def list_archives() -> None:
    result = subprocess.run(["borg", "list"], env=borg_env())
    sys.exit(result.returncode)


def verify_repo() -> None:
    repo = borg_repo()
    print(f"Verifying {repo} ...")
    result = subprocess.run(["borg", "check", "-v", "--show-rc"], env=borg_env())
    sys.exit(result.returncode)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Backup stack configs and secrets to Hetzner Storage Box via BorgBackup"
    )
    parser.add_argument("--list", action="store_true", help="List archives")
    parser.add_argument("--verify", action="store_true", help="Verify repository integrity")
    parser.add_argument("--init", action="store_true", help="Initialize borg repo")
    args = parser.parse_args()

    if args.init:
        init_repo()
        return

    if args.list:
        list_archives()
        return

    if args.verify:
        verify_repo()
        return

    run_backup()


if __name__ == "__main__":
    main()
