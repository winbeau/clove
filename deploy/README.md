# Deploy

Runtime configs for the personal VPS deployment of Clove.

This setup terminates TLS in Caddy on port **8443** (because port 443 is held
by Xray REALITY on this host). Friend's clients use
`https://claude.selab.top:8443`.

The TLS cert is issued out-of-band by `acme.sh` via DNSPod DNS-01 and dropped
into `deploy/certs/` (gitignored) — Caddy reads it from there. acme.sh's cron
auto-renews and hooks `caddy reload` on success.

## One-time setup

### 1. Issue the cert (already done if you reached `Cert success.`)

```bash
~/.acme.sh/acme.sh --set-default-ca --server letsencrypt
 export DPI_Id='...'   # DNSPod International API ID
 export DPI_Key='...'  # DNSPod International API Key
~/.acme.sh/acme.sh --issue --dns dns_dpi -d claude.selab.top
```

### 2. Install cert to a stable path + register reload hook

```bash
mkdir -p /home/winbeau/clove/deploy/certs
~/.acme.sh/acme.sh --install-cert -d claude.selab.top --ecc \
  --key-file       /home/winbeau/clove/deploy/certs/claude.selab.top.key \
  --fullchain-file /home/winbeau/clove/deploy/certs/claude.selab.top.crt \
  --reloadcmd      'systemctl --user reload caddy 2>/dev/null || sudo systemctl reload caddy'
chmod 600 /home/winbeau/clove/deploy/certs/claude.selab.top.key
```

acme.sh writes a record in `~/.acme.sh/account.conf`; its daily cron picks
this up and re-runs the install + reload on renewal.

### 3. Install Caddy binary

```bash
# /tmp/caddy is the vanilla Caddy you downloaded earlier
sudo install -m 0755 -o root -g root /tmp/caddy /usr/local/bin/caddy
caddy version
```

### 4. Drop in Caddyfile + systemd unit

```bash
sudo install -m 0644 deploy/Caddyfile     /etc/caddy/Caddyfile
sudo install -m 0644 deploy/caddy.service /etc/systemd/system/caddy.service
sudo systemctl daemon-reload
sudo systemctl enable --now caddy
sudo systemctl status caddy --no-pager
```

### 5. Install Clove systemd unit

```bash
sudo install -m 0644 deploy/clove.service /etc/systemd/system/clove.service
sudo systemctl daemon-reload
sudo systemctl enable --now clove
sudo systemctl status clove --no-pager
```

If a stale `nohup` Clove is still running, kill it first:
`pgrep -af 'app.main' | head; kill <PID>`.

## Verification

```bash
# 1. local
curl -sS http://127.0.0.1:5201/api/admin/accounts -H "x-api-key: $ADMIN_KEY"

# 2. via Caddy on 8443 (still local)
curl -sS https://claude.selab.top:8443/api/admin/accounts -H "x-api-key: $ADMIN_KEY"

# 3. from your friend's machine
ANTHROPIC_BASE_URL=https://claude.selab.top:8443 \
ANTHROPIC_API_KEY=$FRIEND_KEY \
  claude  # or whatever client
```

## Files

- `Caddyfile` — Caddy config: TLS on :8443 with static cert, reverse proxy to
  127.0.0.1:5201, `auto_https off` because we manage certs ourselves.
- `caddy.service` — runs Caddy as `winbeau` user (port 8443 is unprivileged).
- `clove.service` — runs Clove via the uv-managed venv, with `.env` from the
  repo. Sandboxed so it can only write under `/home/winbeau/clove` and
  `/home/winbeau/.clove`.
- `certs/` — gitignored, written by `acme.sh --install-cert`, renewed by its
  daily cron (which also fires the `--reloadcmd` to reload Caddy).
