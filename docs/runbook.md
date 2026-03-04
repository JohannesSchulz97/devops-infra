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
