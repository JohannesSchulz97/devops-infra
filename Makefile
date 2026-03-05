# devops-infra Makefile
# Load .env if present
-include .env
export

SSH_USER ?= <admin-user>
SERVER := $(SSH_USER)@<server-ip>

.PHONY: status stop-all start-all backup-pg backup-configs backup-r2 disk certs

# Show all running containers
status:
	docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'

# Stop all Docker Compose stacks
stop-all:
	@for dir in /opt/twenty /opt/n8n /opt/dagster /opt/SurfSense /opt/llm-pipelines /opt/listmonk /opt/watchtower; do \
		echo "Stopping $$dir..."; \
		cd $$dir && docker compose down 2>/dev/null || true; \
	done
	@echo "Stopping coder..."
	sudo systemctl stop coder || true

# Start all Docker Compose stacks
start-all:
	@for dir in /opt/twenty /opt/n8n /opt/dagster /opt/SurfSense /opt/llm-pipelines /opt/listmonk /opt/watchtower; do \
		echo "Starting $$dir..."; \
		cd $$dir && docker compose up -d 2>/dev/null || true; \
	done
	@echo "Starting coder..."
	sudo systemctl start coder || true

# Backup all production PostgreSQL databases to Storage Box
backup-pg:
	uv run python scripts/backup_pg.py --production

# Backup all PostgreSQL databases (including non-production)
backup-pg-all:
	uv run python scripts/backup_pg.py --all

# Backup stack configs and secrets to Storage Box
backup-configs:
	sudo uv run python scripts/backup_configs.py

# Backup Foundry datasets to Cloudflare R2
backup-r2:
	uv run python scripts/backup_foundry_datasets_cfr2.py

# Show disk usage
disk:
	@echo "=== Root filesystem ==="
	df -h /
	@echo
	@echo "=== Database volumes ==="
	du -sh /mnt/main/pg_* 2>/dev/null || true
	@echo
	@echo "=== Storage Box ==="
	df -h /mnt/storagebox 2>/dev/null || echo "(not mounted)"
	@echo
	@echo "=== Docker ==="
	docker system df

# Show SSL certificate status
certs:
	sudo certbot certificates
