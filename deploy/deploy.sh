#!/usr/bin/env bash
set -euo pipefail

REMOTE="lc-main-root"
DEPLOY_DIR="/var/www/mielenosoitukset-telegram-bot"
REPO_URL="https://github.com/mielenosoitukset-fi/mielenosoitukset-telegram-bot.git"

echo "==> Deploying mielenosoitukset-telegram-bot to $REMOTE"

ssh "$REMOTE" bash -s <<'REMOTE_SCRIPT'
set -euo pipefail
DEPLOY_DIR="/var/www/mielenosoitukset-telegram-bot"
REPO_URL="https://github.com/mielenosoitukset-fi/mielenosoitukset-telegram-bot.git"

# 1. Clone or pull
if [ -d "$DEPLOY_DIR/.git" ]; then
  echo "==> Pulling latest code"
  cd "$DEPLOY_DIR"
  git pull --ff-only
else
  echo "==> Cloning repo"
  git clone "$REPO_URL" "$DEPLOY_DIR"
  cd "$DEPLOY_DIR"
fi

# 2. Create venv if missing
if [ ! -d ".venv/bin/activate" ]; then
  echo "==> Creating virtualenv"
  python3 -m venv .venv
fi

# 3. Install/update dependencies
echo "==> Installing dependencies"
.venv/bin/pip install -q -e .

# 4. Create .env if missing
if [ ! -f ".env" ]; then
  echo "==> Creating .env (edit with real tokens!)"
  cat > .env <<'ENVFILE'
TELEGRAM_BOT_TOKEN=__SET_ME__
API_BASE_URL=https://mielenosoitukset.fi/api
API_TOKEN=
POLL_MINUTES=15
ADMIN_CHAT_IDS=
ENVFILE
  echo "    WARNING: .env created — edit /var/www/mielenosoitukset-telegram-bot/.env with real tokens!"
fi

# 5. Ensure data dir exists
mkdir -p data

# 6. Install systemd service
echo "==> Installing systemd service"
cp deploy/mielenosoitukset-telegram-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable mielenosoitukset-telegram-bot.service

# 7. Restart
echo "==> Restarting service"
systemctl restart mielenosoitukset-telegram-bot.service

echo "==> Done. Check status: systemctl status mielenosoitukset-telegram-bot"
REMOTE_SCRIPT

echo "==> Deployment complete"
ssh "$REMOTE" "systemctl status mielenosoitukset-telegram-bot --no-pager | head -15"
