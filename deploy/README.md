# Deploy

Runtime configs for the personal VPS deployment of Clove.

This setup terminates TLS in Caddy on port **8443** (because port 443 is held
by Xray REALITY on this host). Friend's clients use
`https://claude.selab.top:8443`.

Caddy fetches its own Let's Encrypt cert via DNS-01 against Tencent Cloud
DNSPod, using the `caddy-dns/tencentcloud` plugin. Build a custom Caddy
binary once with `xcaddy`, supply Tencent Cloud API credentials via a
secrets file, and Caddy handles issue + auto-renewal itself.

## One-time setup

### 1. Build Caddy with the tencentcloud plugin

```bash
sudo apt update && sudo apt install -y golang-go
go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
export PATH=$PATH:$(go env GOPATH)/bin
xcaddy build --with github.com/caddy-dns/tencentcloud
# the resulting `caddy` binary is in the current directory
```

### 2. Install Caddy binary + secrets

Get Tencent Cloud API credentials at
<https://console.cloud.tencent.com/cam/capi> (CAM access key — SecretId +
SecretKey). The associated CAM user needs the `QcloudDNSPodFullAccess`
policy (or at minimum permission to add/delete TXT records on the zone).

```bash
sudo install -m 0755 -o root -g root ./caddy /usr/local/bin/caddy
caddy version

# Write secrets file readable by the winbeau user only
sudo mkdir -p /etc/caddy
sudo tee /etc/caddy/secrets.env >/dev/null <<'EOF'
TENCENTCLOUD_SECRET_ID=<your_secret_id>
TENCENTCLOUD_SECRET_KEY=<your_secret_key>
EOF
sudo chown root:winbeau /etc/caddy/secrets.env
sudo chmod 0640 /etc/caddy/secrets.env
```

### 3. Drop in Caddyfile + systemd unit

```bash
sudo install -m 0644 deploy/Caddyfile     /etc/caddy/Caddyfile
sudo install -m 0644 deploy/caddy.service /etc/systemd/system/caddy.service
sudo systemctl daemon-reload
sudo systemctl enable --now caddy
sudo systemctl status caddy --no-pager
sudo journalctl -u caddy -n 50 --no-pager   # watch the ACME flow succeed
```

On first boot Caddy will hit Tencent Cloud's API to add a
`_acme-challenge.claude.selab.top` TXT record, wait for Let's Encrypt to
verify it, then clean it up. Subsequent renewals (~60 days) repeat the
same flow with no manual intervention.

### 4. Install Clove systemd unit

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
