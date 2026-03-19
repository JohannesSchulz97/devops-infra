# Runbook

Common operations for the <your-org> main server.

## Connecting

```bash
ssh <admin-user>@<server-ip>
```

## Service Management

### Check all running containers

```bash
docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'
```

### Start/stop a stack

```bash
cd /opt/<stack-name>
docker compose up -d        # start
docker compose down          # stop
docker compose restart       # restart
docker compose logs -f       # follow logs
```

Stack directories on server:
- `/opt/twenty/` — Twenty CRM
- `/opt/n8n/` — n8n
- `/opt/dagster/` — Dagster
- `/opt/SurfSense/` — SurfSense
- `/opt/llm-pipelines/` — LangGraph
- `/opt/listmonk/` — Listmonk
- `/opt/watchtower/` — Watchtower
- `/opt/beszel/` — Beszel monitoring (hub + agent)

### Coder (systemd, not Docker Compose)

```bash
sudo systemctl status coder
sudo systemctl restart coder
sudo journalctl -u coder -f
```

## Backups

### PostgreSQL — all production databases

```bash
cd /opt/foundry-backup   # or wherever scripts are deployed
export $(grep -v '^#' .env | xargs)
uv run python scripts/backup_pg.py --production
```

### PostgreSQL — single profile

```bash
uv run python scripts/backup_pg.py n8n
uv run python scripts/backup_pg.py --list n8n       # list archives
uv run python scripts/backup_pg.py --verify n8n     # verify integrity
```

Available profiles: `foundry-datasets`, `twenty-crm`, `n8n`, `surfsense`, `dagster`, `langgraph`, `listmonk`

### Stack configs

```bash
sudo uv run python scripts/backup_configs.py
sudo uv run python scripts/backup_configs.py --list
```

Note: `backup_configs.py` needs sudo to read all `/opt` files including `.env` secrets.

### Foundry datasets to R2

```bash
uv run python scripts/backup_foundry_datasets_cfr2.py
uv run python scripts/backup_foundry_datasets_cfr2.py --list
```

### Initialize a new Borg repo

When adding a new database profile, initialize its repo first:

```bash
uv run python scripts/backup_pg.py --init <profile-name>
```

## Storage Box

### Check mount status

```bash
mountpoint /mnt/storagebox && echo "mounted" || echo "not mounted"
df -h /mnt/storagebox
```

### Mount/unmount

```bash
sudo systemctl start mnt-storagebox.mount
sudo systemctl stop mnt-storagebox.mount
```

### Manual SSHFS mount (if systemd unit fails)

```bash
sshfs -p 23 <storage-box-id>@<storage-box-id>.your-storagebox.de:foundry-mediasets /mnt/storagebox \
  -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3
```

### Storage Box access (direct)

```bash
sftp -P 23 <storage-box-id>@<storage-box-id>.your-storagebox.de
```

## Cron Backup Scheduling

Backups are scheduled via a cron drop-in file. Status JSON files are written to `/opt/backup-status/` for Dagster observability.

### Initial setup (one-time)

```bash
# Create status directory
sudo mkdir -p /opt/backup-status

# Deploy cron file
sudo cp /opt/devops-infra/cron/devops-backups /etc/cron.d/devops-backups
sudo chmod 644 /etc/cron.d/devops-backups
```

### Check backup status

```bash
# View latest status files
cat /opt/backup-status/pg-*.json /opt/backup-status/configs.json | jq .

# Check cron logs
tail -f /var/log/devops-backup-pg.log
tail -f /var/log/devops-backup-configs.log
```

### Redeploy cron after changes

```bash
sudo cp /opt/devops-infra/cron/devops-backups /etc/cron.d/devops-backups
```

## SSL Certificates

### Check certificate status

```bash
sudo certbot certificates
```

### Force renewal

```bash
sudo certbot renew --force-renewal
sudo systemctl reload nginx
```

### Add a new domain

```bash
sudo certbot certonly --nginx -d newservice.<host-domain>
# Then create nginx config in /etc/nginx/sites-available/
# Symlink to sites-enabled and reload nginx
```

## nginx

### Test configuration

```bash
sudo nginx -t
```

### Reload after config change

```bash
sudo systemctl reload nginx
```

### Add a new service

1. Create `/etc/nginx/sites-available/newservice`
2. `sudo ln -s /etc/nginx/sites-available/newservice /etc/nginx/sites-enabled/`
3. `sudo nginx -t && sudo systemctl reload nginx`

## Database Access

### Connect to a database from the server

```bash
docker exec -it <container> psql -U <user> -d <database>

# Examples:
docker exec -it twenty-db psql -U twenty -d default
docker exec -it n8n-db psql -U n8n -d n8n
docker exec -it dagster-db psql -U dagster -d dagster
```

### Connect remotely via SSH tunnel

```bash
# Terminal 1: open tunnel
ssh -N -L 5435:127.0.0.1:5435 <admin-user>@<server-ip>

# Terminal 2: connect
psql -h 127.0.0.1 -p 5435 -U twenty -d default
```

## Disk Space

### Check usage

```bash
df -h /
du -sh /mnt/main/pg_*            # database sizes
du -sh /var/lib/docker/           # total Docker usage
docker system df                  # Docker disk usage breakdown
```

### Clean up Docker

```bash
docker system prune -f            # remove stopped containers, unused networks
docker image prune -a -f          # remove all unused images (careful!)
```

## Deploying Config Changes

After editing compose files or nginx configs in this repo:

```bash
# Sync a compose file to server
scp stacks/n8n/docker-compose.yml <admin-user>@<server-ip>:/opt/n8n/docker-compose.yml

# Sync an nginx config
scp nginx/n8n.conf <admin-user>@<server-ip>:/etc/nginx/sites-available/n8n

# On server: apply changes
ssh <admin-user>@<server-ip>
cd /opt/n8n && docker compose up -d    # recreates changed containers
sudo nginx -t && sudo systemctl reload nginx
```

## Twenty CRM: Full Workspace Recovery After Data Loss

Use this when the Twenty workspace schema is missing or corrupted (e.g. someone dropped it, or `core.objectMetadata` is empty). Symptoms: login hangs on `/welcome`, server logs show `workspaceMember is missing` or `No role found for userWorkspace`.

> **Background**: The workspace schema (`workspace_<slug>`) is NOT created by the upgrade command. It is only created by the `activateWorkspace` GraphQL mutation via `WorkspaceManagerService.init()`. The upgrade command only runs data migrations on *existing* schemas.

### 1. Back up existing users

```bash
docker exec twenty-db psql -U twenty -d default -c \
  "COPY (SELECT id, email, \"passwordHash\", \"defaultWorkspaceId\", \"isEmailVerified\", \"createdAt\" FROM core.user) TO STDOUT WITH CSV HEADER" \
  > /opt/twenty/users_backup_$(date +%Y%m%d).csv
```

### 2. Reset the core schema (if corrupted)

Stop app containers first, then drop and recreate:

```bash
cd /opt/twenty
docker compose stop twenty-server twenty-worker

# Drop corrupted schemas
docker exec twenty-db psql -U twenty -d default -c "DROP SCHEMA core CASCADE;"

# Restart server — entrypoint will recreate core schema + run migrations
docker compose up -d twenty-server
docker compose logs -f twenty-server   # wait for "Nest application successfully started"
```

### 3. Create a new workspace via API

The workspace schema is created by calling `activateWorkspace`. Do this from the server against localhost to bypass Cloudflare Access:

```bash
# Step 1: Sign up a temporary user to get a loginToken
curl -s -X POST http://localhost:3003/metadata \
  -H "Content-Type: application/json" \
  -d '{"query":"mutation { signUpInWorkspace(email: \"setup@<host-domain>\", password: \"<temp-password>\", workspaceDisplayName: \"setup\") { loginToken { token } workspace { id } } }"}' | jq .

# Step 2: Exchange loginToken for an access token
curl -s -X POST http://localhost:3003/metadata \
  -H "Content-Type: application/json" \
  -d '{"query":"mutation { getAuthTokensFromLoginToken(loginToken: \"<TOKEN>\", origin: \"http://localhost:3003\") { tokens { accessOrWorkspaceAgnosticToken { token } } } }"}' | jq .

# Step 3: Activate the workspace (creates the workspace_<slug> schema with all 30 tables)
curl -s -X POST http://localhost:3003/metadata \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -d '{"query":"mutation { activateWorkspace(data: { displayName: \"<your-org> AG\" }) { id subdomain activationStatus version } }"}' | jq .
```

Note the new workspace ID from step 1 — you'll need it below.

### 4. Restore users

```bash
docker exec -i twenty-db psql -U twenty -d default <<'SQL'
-- Delete temp setup user
DELETE FROM core.user WHERE email = 'setup@<host-domain>';

-- Re-insert real users with their original bcrypt password hashes
INSERT INTO core.user (id, email, "passwordHash", "defaultWorkspaceId", "isEmailVerified", "createdAt", "updatedAt")
VALUES
  ('<id>', '<email>', '<hash>', '<new-workspace-id>', true, now(), now()),
  ...;

-- Link users to the new workspace
INSERT INTO core."userWorkspace" (id, "userId", "workspaceId", "createdAt", "updatedAt")
VALUES
  (gen_random_uuid(), '<userId>', '<new-workspace-id>', now(), now()),
  ...;
SQL
```

### 5. Create workspaceMember records

Each user needs a record in the workspace schema:

```bash
docker exec -i twenty-db psql -U twenty -d default <<SQL
INSERT INTO workspace_<slug>."workspaceMember"
  (id, "nameFirstName", "nameLastName", "colorScheme", "userId", "createdAt", "updatedAt")
VALUES
  (gen_random_uuid(), 'First', 'Last', 'Light', '<userId>', now(), now()),
  ...;
SQL
```

### 6. Assign roles

```bash
docker exec -i twenty-db psql -U twenty -d default <<SQL
-- Find role IDs
SELECT id, label FROM core.role;

-- Find application ID
SELECT id FROM core."keyValuePair" WHERE key = 'STANDARD_OBJECTS_CREATED' LIMIT 1;
-- Application ID is in core."application" table:
SELECT id FROM core.application LIMIT 1;

-- Assign roles (one row per userWorkspace)
INSERT INTO core."roleTarget" (id, "roleId", "userWorkspaceId", "workspaceId", "createdAt", "updatedAt")
SELECT gen_random_uuid(), '<admin-role-id>', uw.id, uw."workspaceId", now(), now()
FROM core."userWorkspace" uw
JOIN core.user u ON uw."userId" = u.id
WHERE u.email = '<admin-email>';

-- Repeat with member-role-id for other users
SQL
```

### 7. Flush cache and start worker

```bash
docker exec twenty-twenty-server-1 yarn command:prod cache:flush
docker compose up -d twenty-worker
```

Login should now work. Verify: `curl http://localhost:3003/healthz`

---

## Monitoring (Beszel)

Beszel provides server metrics, Docker container stats, and alerting.

- **URL**: https://monitor.<host-domain>
- **Stack**: `/opt/beszel/`
- **Architecture**: Hub (web UI + data storage) + Agent (metrics collector), both on <server-host>
- **Hub ↔ Agent communication**: Unix socket via shared Docker volume (no TCP exposure)
- **Data**: SQLite in `beszel-data` named volume

### Access

Open https://monitor.<host-domain> and log in. Alert rules are configured through the web UI.

### Reconfigure the agent

If you need to regenerate the agent key (e.g. after re-adding the system in the hub):

```bash
cd /opt/beszel
# Update BESZEL_KEY in .env with the new key from the hub's "Add System" dialog
nano .env
docker compose up -d beszel-agent
```

### Check status

```bash
docker ps --filter name=beszel
curl -s http://localhost:8090/api/health
```

## Logging (journald)

Docker uses the **journald** log driver. All container logs are sent to systemd-journald for unified, searchable logging.

### Query container logs

```bash
# Recent logs for a specific container
journalctl CONTAINER_NAME=twenty-db --since "1 hour ago"

# Follow/tail a container's logs
journalctl CONTAINER_NAME=twenty-db -f

# Search all logs for a pattern
journalctl --since "2026-03-01" | grep -i "xfs"

# Kernel messages only
journalctl -k | grep -i "error"
```

### Manage journal storage

```bash
# Check disk usage
journalctl --disk-usage

# Manually vacuum old logs
sudo journalctl --vacuum-size=1G
sudo journalctl --vacuum-time=60d
```

### Configuration

- **Log driver**: `/etc/docker/daemon.json` — `"log-driver": "journald"` with `"tag": "{{.Name}}"`
- **Retention**: `/etc/systemd/journald.conf` — `SystemMaxUse=2G`, `MaxRetentionSec=90d`
- `docker logs <container>` still works as before — no change to existing workflows
- The journald driver only applies to containers created **after** the config change. Existing containers keep their previous driver until recreated.

## Troubleshooting

### Container won't start

```bash
docker compose logs <service-name>     # check logs
docker compose config                  # validate compose file
docker inspect <container>             # check configuration
```

### Storage Box disconnected

```bash
sudo systemctl restart mnt-storagebox.mount
# If that fails:
sudo fusermount -u /mnt/storagebox
sudo systemctl start mnt-storagebox.mount
```

### Borg backup fails with lock

```bash
# If a previous backup was interrupted:
BORG_REPO="ssh://<storage-box-id>@<storage-box-id>.your-storagebox.de:23/./<repo>" \
BORG_PASSPHRASE="..." \
borg break-lock
```
