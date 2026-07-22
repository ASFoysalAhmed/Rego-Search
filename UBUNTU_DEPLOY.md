# Ubuntu Deployment Guide (Production)

This setup runs your API in production on Ubuntu with headed Playwright behavior using Xvfb (virtual display), so you do not need Windows Server.

## What this gives you

- Always-on service with systemd
- Nginx reverse proxy on port 80
- Headed browser behavior via Xvfb
- Production WSGI server (Waitress)

## 1) Provision Ubuntu host

- Ubuntu 22.04 or 24.04
- 2 vCPU, 4 GB RAM minimum
- Open inbound port 80 (and 443 if you add TLS)

## 2) Upload project

Copy your project folder to the Ubuntu server first.

Example target path after copy:
- /home/ubuntu/Rego-search

## 3) Run installer

From your project root on Ubuntu:

```bash
cd /home/ubuntu/Rego-search
sudo bash deploy/ubuntu/install_ubuntu.sh
```

What installer does:
- installs OS packages
- creates service user carma
- copies app into /opt/carma-lookup
- creates venv and installs Python deps
- installs Playwright Chromium + system deps
- installs systemd unit
- installs Nginx site config
- starts and enables services

## 4) Configure runtime env

Edit environment file:

```bash
sudo nano /etc/carma-lookup.env
```

Recommended values:
- FORCE_HEADED_BROWSER=true
- DEFAULT_USE_HEADLESS_BROWSER=false
- MAX_BROWSER_SESSIONS=1
- BROWSER_QUEUE_SIZE=4
- PLAYWRIGHT_BROWSERS_PATH=/opt/carma-lookup/.ms-playwright
- REQUIRE_API_TOKEN=true
- API_TOKEN=<long-random-secret>

Apply changes:

```bash
sudo systemctl restart carma-lookup
```

## 5) Verify

```bash
systemctl status carma-lookup --no-pager
systemctl status nginx --no-pager
curl http://127.0.0.1/health
curl http://127.0.0.1/browser-status
```

## 6) Test lookup

```bash
curl -X POST http://127.0.0.1/lookup \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <API_TOKEN>" \
  -d '{
    "search_type": "byRego",
    "rego": "YQO87S",
    "state": "ACT",
    "max_browser_seconds": 120
  }'
```

## 7) Operations commands

Logs:

```bash
journalctl -u carma-lookup -f
```

Restart service:

```bash
sudo systemctl restart carma-lookup
```

Check Nginx config:

```bash
sudo nginx -t
```

## 8) TLS (recommended)

Use Certbot with Nginx:

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example
```

## 9) Scaling notes

- Scale vertically first: keep MAX_BROWSER_SESSIONS low.
- For more throughput, scale horizontally with multiple VMs and load balancing.
- Avoid high local browser concurrency; it increases failures and memory pressure.
