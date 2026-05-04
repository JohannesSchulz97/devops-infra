# LiteLLM stack

Centralized LiteLLM proxy for cost tracking and provider abstraction.
Internal endpoint: `http://127.0.0.1:4000` (Docker host).
Public endpoint: `https://llm.<host-domain>` (via Cloudflare Tunnel + nginx).

## Files

- `docker-compose.yml` / `docker-compose.prod.yml` — LiteLLM proxy + Postgres
- `litellm_config.yaml` — model + virtual key configuration
- `nginx.conf` — internal load-balancer between proxy replicas (port 4000)
- `nginx-external.conf` — host-level nginx site for `llm.<host-domain>` (TLS + reverse proxy to 127.0.0.1:4000). Deployed to `/etc/nginx/sites-available/litellm-external`.
- `.env.example` — required env vars (master key, DB password, etc.)

## Public endpoint architecture

Hetzner firewall blocks inbound 80/443. All public <host-domain> subdomains route via
Cloudflare Tunnel:

```
client → Cloudflare edge → Cloudflare Tunnel (cloudflared on <server-host>)
       → https://127.0.0.1:443 (host nginx)
       → http://127.0.0.1:4000 (LiteLLM container)
```

DNS for `llm.<host-domain>` is a CNAME to `<tunnel-uuid>.cfargotunnel.com` (proxied/orange).

## Adding a new subdomain to cloudflared

1. Edit `/etc/cloudflared/config.yml` on `<server-host>`. Insert a new ingress block
   **before** the catch-all `- service: http_status:404`:
   ```yaml
   - hostname: <new>.<host-domain>
     service: https://127.0.0.1:443
     originRequest:
       noTLSVerify: true
       originServerName: <new>.<host-domain>
   ```
2. Restart the tunnel (the service does not support `reload`):
   ```bash
   sudo systemctl restart cloudflared
   sudo journalctl -u cloudflared -n 20 --no-pager
   ```
3. In Cloudflare DNS for `<host-domain>`, add a **CNAME** record:
   - Name: `<new>`
   - Target: `<tunnel-uuid>.cfargotunnel.com` (same UUID as existing entries; see top of `config.yml`)
   - Proxy: **Proxied (orange cloud)**
4. Add an nginx server block on the host with `server_name <new>.<host-domain>` and a
   matching SSL cert (see renewal section below).
5. If the wildcard `*.<host-domain>` Cloudflare Access app is in place, create a Bypass
   app for `<new>.<host-domain>` so it isn't forced through SSO.

## SSL certificate renewal (manual, DNS-01)

The cert for `llm.<host-domain>` was issued via Let's Encrypt DNS-01 challenge because
ports 80/443 are not reachable from the public internet (firewalled at Hetzner;
HTTP-01 cannot complete). Autorenewal is **NOT configured** — renewals must be
performed manually before the cert expires.

### Renewal procedure

1. SSH to `<server-host>`. Check expiry:
   ```bash
   sudo certbot certificates | grep -A4 llm.<host-domain>
   ```
2. Run the manual DNS-01 renewal:
   ```bash
   sudo certbot certonly --manual --preferred-challenges dns -d llm.<host-domain>
   ```
3. Certbot prints a TXT record value. In Cloudflare DNS for `<host-domain>`, add:
   - Type: `TXT`
   - Name: `_acme-challenge.llm`
   - Value: (string from certbot)
   - TTL: `Auto` (or 60s)
4. Wait ~30s for propagation, verify:
   ```bash
   dig +short TXT _acme-challenge.llm.<host-domain> @1.1.1.1
   ```
5. Press Enter in certbot to complete the challenge.
6. Reload nginx so it picks up the new cert:
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```
7. Delete the `_acme-challenge.llm` TXT record from Cloudflare (no longer
   needed and clutters DNS).

### Verifying the cert

```bash
echo | openssl s_client -connect llm.<host-domain>:443 -servername llm.<host-domain> 2>/dev/null \
  | openssl x509 -noout -dates -subject
curl -sI https://llm.<host-domain>/health/liveliness
```

### TODO: automate renewal

The manual DNS-01 process should be replaced by the certbot Cloudflare DNS
plugin (`python3-certbot-dns-cloudflare`) using a scoped Cloudflare API token,
which would allow unattended renewal via the existing certbot timer.
