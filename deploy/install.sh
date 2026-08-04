#!/usr/bin/env bash
# One-time bootstrap for the Oracle Cloud (Ubuntu, ARM/aarch64) VM.
# Run from inside the cloned repo at /opt/telegram-bots after copying .env
# and the runtime JSON files onto the server. Idempotent-ish: safe to re-run.
set -euo pipefail

REPO_DIR="/opt/telegram-bots"
PY="python3.14"

cd "$REPO_DIR"

echo "==> Installing Python 3.14 (deadsnakes) + venv/tools"
if ! command -v "$PY" >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update
    sudo apt-get install -y python3.14 python3.14-venv
fi

echo "==> Creating virtualenv (.venv) and installing requirements"
"$PY" -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "==> Sanity check: .env and required files present"
[ -f .env ] || { echo "!! .env missing in $REPO_DIR — copy it from your laptop first"; exit 1; }

echo "==> Installing systemd template unit"
sudo cp deploy/telegram-bot@.service /etc/systemd/system/
sudo systemctl daemon-reload

BOTS=(
    imageboard_search_bot
    imageboard_notifier_bot
    example_posting_bot
)

echo "==> Enabling + starting all bots"
echo "    (make sure the SAME bots are STOPPED on your laptop first — Telegram"
echo "     rejects two pollers on one token with a 409 Conflict)"
for bot in "${BOTS[@]}"; do
    sudo systemctl enable --now "telegram-bot@${bot}"
done

echo
echo "==> Done. Check status with:"
echo "    systemctl status 'telegram-bot@*'"
echo "    journalctl -u telegram-bot@example_posting_bot -f"
