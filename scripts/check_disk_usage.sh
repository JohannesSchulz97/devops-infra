#!/usr/bin/env bash
# Check disk usage on extra filesystems and alert to Slack when thresholds are exceeded.
#
# Usage:
#   check_disk_usage.sh                  # run check (uses SLACK_WEBHOOK from environment or /opt/beszel/.env)
#   check_disk_usage.sh --dry-run        # print alerts to stdout without sending to Slack
#
# Cron entry (/etc/cron.d/check-disk-usage):
#   0 */6 * * * root /opt/scripts/check_disk_usage.sh >> /var/log/check-disk-usage.log 2>&1

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Filesystem thresholds (mount_point:threshold_percent:label)
CHECKS=(
    "/mnt/main:80:main Volume"
    "/mnt/storagebox:70:Storagebox"
    "/:90:Root SSD"
)

# Load Slack webhook from environment or beszel .env
if [[ -z "${SLACK_WEBHOOK:-}" ]]; then
    if [[ -f /opt/beszel/.env ]]; then
        SLACK_WEBHOOK=$(grep -oP 'BESZEL_SLACK_WEBHOOK=\K.*' /opt/beszel/.env)
    fi
fi

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

alerts=()

for check in "${CHECKS[@]}"; do
    IFS=: read -r mount threshold label <<< "$check"

    # Skip if not mounted
    if ! mountpoint -q "$mount" 2>/dev/null && [[ "$mount" != "/" ]]; then
        echo "$(date -Iseconds) WARNING: $mount is not mounted, skipping"
        continue
    fi

    usage=$(df "$mount" --output=pcent | tail -1 | tr -d ' %')

    if (( usage >= threshold )); then
        alerts+=("$label ($mount) is at ${usage}% (threshold: ${threshold}%)")
    fi
done

if (( ${#alerts[@]} == 0 )); then
    echo "$(date -Iseconds) OK: all filesystems within thresholds"
    exit 0
fi

# Build Slack message
text="*<server-host> disk alert*"
for alert in "${alerts[@]}"; do
    text="$text\n:warning: $alert"
done

if [[ "$DRY_RUN" == true ]]; then
    echo "$(date -Iseconds) ALERT (dry-run):"
    for alert in "${alerts[@]}"; do
        echo "  - $alert"
    done
    exit 0
fi

if [[ -z "${SLACK_WEBHOOK:-}" ]]; then
    echo "$(date -Iseconds) ERROR: no SLACK_WEBHOOK configured, cannot send alert" >&2
    echo "  Alerts:" >&2
    for alert in "${alerts[@]}"; do
        echo "  - $alert" >&2
    done
    exit 1
fi

payload=$(printf '{"text": "%s"}' "$text")
http_code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d "$payload" "$SLACK_WEBHOOK")

if [[ "$http_code" == "200" ]]; then
    echo "$(date -Iseconds) ALERT sent to Slack:"
    for alert in "${alerts[@]}"; do
        echo "  - $alert"
    done
else
    echo "$(date -Iseconds) ERROR: Slack returned HTTP $http_code" >&2
    exit 1
fi
