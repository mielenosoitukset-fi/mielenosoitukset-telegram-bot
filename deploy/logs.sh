#!/usr/bin/env bash
# Tail bot logs from the server.
# Usage: ./deploy/logs.sh
set -euo pipefail
ssh lc-main-root "journalctl -u mielenosoitukset-telegram-bot -f --no-pager"
