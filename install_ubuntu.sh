#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/carma-lookup"
APP_USER="carma"
PYTHON_BIN="python3"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/ubuntu/install_ubuntu.sh"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y \
  git \
  nginx \
  rsync \
  xvfb \
  python3 \
  python3-venv \
  python3-pip \
  ca-certificates \
  curl

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash "${APP_USER}"
fi

mkdir -p "${APP_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# Copy project files into /opt/carma-lookup. Run this script from repo root.
rsync -a --delete --exclude ".venv" --exclude "__pycache__" ./ "${APP_DIR}/"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

sudo -u "${APP_USER}" bash -lc "cd '${APP_DIR}' && ${PYTHON_BIN} -m venv .venv"
sudo -u "${APP_USER}" bash -lc "cd '${APP_DIR}' && .venv/bin/pip install --upgrade pip"
sudo -u "${APP_USER}" bash -lc "cd '${APP_DIR}' && .venv/bin/pip install -r requirements.txt"
sudo -u "${APP_USER}" bash -lc "cd '${APP_DIR}' && PLAYWRIGHT_BROWSERS_PATH='${APP_DIR}/.ms-playwright' .venv/bin/python -m playwright install --with-deps chromium"

chmod +x "${APP_DIR}/deploy/ubuntu/start_carma_lookup.sh"

if [[ ! -f /etc/carma-lookup.env ]]; then
  cp "${APP_DIR}/deploy/ubuntu/carma-lookup.env.example" /etc/carma-lookup.env
  chmod 640 /etc/carma-lookup.env
  chown root:"${APP_USER}" /etc/carma-lookup.env
fi

cp "${APP_DIR}/deploy/ubuntu/carma-lookup.service" /etc/systemd/system/carma-lookup.service
systemctl daemon-reload
systemctl enable carma-lookup.service
systemctl restart carma-lookup.service

cp "${APP_DIR}/deploy/ubuntu/nginx.carma-lookup.conf" /etc/nginx/sites-available/carma-lookup
ln -sf /etc/nginx/sites-available/carma-lookup /etc/nginx/sites-enabled/carma-lookup
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl restart nginx

echo "Install complete."
echo "Check service: systemctl status carma-lookup --no-pager"
echo "Check API: curl http://127.0.0.1/health"
