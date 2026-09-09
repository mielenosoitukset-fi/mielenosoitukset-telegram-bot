#!/usr/bin/env bash
# Quick helper: set the .env values on the server via SSH.
# Usage:
#   ./deploy/set-env.sh TELEGRAM_BOT_TOKEN=xxx
#   ./deploy/set-env.sh API_TOKEN=yyy ADMIN_CHAT_IDS=123456
set -euo pipefail

REMOTE="lc-main-root"
ENV_FILE="/var/www/mielenosoitukset-telegram-bot/.env"

if [ $# -eq 0 ]; then
  echo "Usage: $0 KEY=VALUE [KEY=VALUE ...]"
  echo "Supported keys: TELEGRAM_BOT_TOKEN, API_TOKEN, API_BASE_URL, POLL_MINUTES, ADMIN_CHAT_IDS"
  exit 1
fi

ssh "$REMOTE" bash -s <<REMOTE_SCRIPT
set -euo pipefail
ENV_FILE="$ENV_FILE"

# Ensure file exists
touch "\$ENV_FILE"

for pair in "$@"; do
  key="\${pair%%=*}"
  val="\${pair#*=}"
  # Remove existing key line, then append
  sed -i "/^"\$key"=/d" "\$ENV_FILE"
  echo "\$key=\$val" >> "\$ENV_FILE"
  echo "Set \$key"
done
REMOTE_SCRIPT

echo "==> .env updated. Restart with: ssh $REMOTE 'systemctl restart mielenosoitukset-telegram-bot'"
