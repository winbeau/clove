# Deploy

Runtime configs for the personal VPS deployment of Clove.

## `clove.service`

Systemd unit that runs `app.main` via the uv-managed venv. Install:

```bash
sudo install -m 0644 deploy/clove.service /etc/systemd/system/clove.service
sudo systemctl daemon-reload
sudo systemctl enable --now clove
sudo systemctl status clove
```

Logs go to journalctl (`journalctl -u clove -f`) plus the file path set in
`.env` (`LOG_FILE_PATH`). The unit binds to whatever `HOST`/`PORT` are set in
the project's `.env` — keep `HOST=127.0.0.1` and let Caddy front the public
side.

The unit assumes `uv` lives at `/home/winbeau/.local/bin/uv` and the venv at
`/home/winbeau/clove/.venv`. Edit `ExecStart` if your layout differs.

## `Caddyfile`

Reverse-proxies `https://claude.selab.top` to `127.0.0.1:5201` and handles TLS
via Let's Encrypt automatically. Install:

```bash
# Install Caddy (Debian/Ubuntu)
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

# Drop in our config and reload
sudo install -m 0644 deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo journalctl -u caddy -f   # watch ACME succeed
```

Prereqs: DNS A record for `claude.selab.top` → this VPS, ports 80/443 open,
and (if behind Cloudflare) the record set to **DNS only** so Caddy can complete
the ACME HTTP-01 challenge and Clove's outbound traffic isn't double-proxied.
