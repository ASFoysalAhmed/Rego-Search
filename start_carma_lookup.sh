#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/carma-lookup"

cd "${APP_DIR}"

# Source env injected by systemd (if provided).
if [[ -f /etc/carma-lookup.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /etc/carma-lookup.env
  set +a
fi

# Keep headed browser behavior while using a virtual display.
exec /usr/bin/xvfb-run -a \
  --server-args="-screen 0 1366x900x24" \
  "${APP_DIR}/.venv/bin/python" "${APP_DIR}/run_prod.py"
