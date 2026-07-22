# Carma Lookup API Production Guide

This guide is for running the API in production with headed browser mode (recommended for this target site).

## 1) Production architecture

- API process: Flask app served by Waitress
- Browser automation: Playwright Chromium in headed mode
- Concurrency control: built-in worker queue in app
- Reverse proxy: IIS or Nginx in front of API (recommended)

## 2) Prerequisites

- Windows Server/Desktop with an interactive user session
- Python 3.10+ (3.11 recommended)
- Access to run a persistent logged-in desktop session

Why interactive session matters:
- Headed browser mode needs desktop rendering. If the session is logged out, headed automation can fail.

## 3) Install dependencies

```powershell
cd "C:\Users\Admin\Downloads\Rego search"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## 4) Configure environment

Copy and edit the production env file:

```powershell
Copy-Item .env.production.example .env.production
```

Recommended values:
- FORCE_HEADED_BROWSER=true
- DEFAULT_USE_HEADLESS_BROWSER=false
- MAX_BROWSER_SESSIONS=1
- BROWSER_QUEUE_SIZE=4
- REQUIRE_API_TOKEN=true
- API_TOKEN=<long-random-secret>

Notes:
- Start with MAX_BROWSER_SESSIONS=1 for stability.
- Increase to 2 only after observing stable memory/CPU and low error rate.

## 5) Run production server

```powershell
$env:API_HOST="0.0.0.0"
$env:API_PORT="5000"
$env:API_DEBUG="false"
$env:REQUIRE_API_TOKEN="true"
$env:API_TOKEN="replace-with-a-long-random-secret"
$env:FORCE_HEADED_BROWSER="true"
$env:DEFAULT_USE_HEADLESS_BROWSER="false"
$env:MAX_BROWSER_SESSIONS="1"
$env:BROWSER_QUEUE_SIZE="4"
$env:WAITRESS_THREADS="8"

.\.venv\Scripts\python.exe run_prod.py
```

Health checks:

```powershell
curl http://127.0.0.1:5000/health
curl http://127.0.0.1:5000/browser-status
```

## 6) API usage

### Rego lookup

```http
POST /lookup
Content-Type: application/json
Authorization: Bearer <API_TOKEN>

{
  "search_type": "byRego",
  "rego": "YQO87S",
  "state": "ACT",
  "max_browser_seconds": 120
}
```

### VIN lookup

```http
POST /lookup
Content-Type: application/json
Authorization: Bearer <API_TOKEN>

{
  "search_type": "byVin",
  "vin": "MRHGM26409P050012",
  "max_browser_seconds": 120
}
```

Important:
- Even if client sends use_headless_browser=true, FORCE_HEADED_BROWSER=true will keep production in headed mode.
- Protected endpoints require either Authorization: Bearer <API_TOKEN> or X-API-Token: <API_TOKEN>.

## 7) Run as a persistent process on Windows

Options:
- Task Scheduler (Run only when user is logged on) with restart on failure
- NSSM wrapping the same run command under a dedicated user account

Recommended:
- Use a dedicated local user for this API.
- Auto-login that user on server restart if policy allows.
- Keep RDP session active (avoid sign-out; disconnect is usually fine).

## 8) Reverse proxy

Put IIS/Nginx in front of API for:
- TLS termination
- rate limiting
- request size/time controls
- centralized access logs

Set proxy timeout >= max_browser_seconds + 20s.

## 9) Monitoring and tuning

Monitor these continuously:
- /browser-status: queued_jobs, active_jobs, alive_workers
- API error rate for 422/503
- System memory and CPU

Tuning sequence:
1. Keep MAX_BROWSER_SESSIONS=1.
2. Increase BROWSER_QUEUE_SIZE before increasing sessions.
3. Increase sessions to 2 only if machine resources are healthy and errors stay low.

## 10) Troubleshooting

### Browser returns missing fields
- Ensure FORCE_HEADED_BROWSER=true.
- Confirm process is running in an interactive desktop session.
- Check if security software blocks Chromium child processes.

### Queue full / timeout
- Increase max_browser_seconds in request.
- Increase BROWSER_QUEUE_SIZE moderately.
- Scale out to another host instead of large local concurrency.

### Browser process leaks
- Keep current worker reset behavior.
- Restart service during low-traffic windows daily if needed.

## 11) Security checklist

- Put API behind authenticated gateway (IP allowlist or token auth).
- Enforce HTTPS at reverse proxy.
- Restrict inbound ports to proxy only.
- Rotate logs and monitor failed requests.

## 12) Minimal production start checklist

- Dependencies installed
- Playwright Chromium installed
- FORCE_HEADED_BROWSER=true
- Running with run_prod.py (Waitress)
- /health and /browser-status are healthy
- Reverse proxy configured with TLS and timeout
