#!/usr/bin/env bash
# JobPilot one-command setup for Linux / macOS.
# Usage (from the project folder):  ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n\033[36m==> %s\033[0m\n' "$1"; }

case "$PWD" in
  *OneDrive*|*Dropbox*|*"Google Drive"*|*iCloud*)
    echo "WARNING: this folder is cloud-synced ($PWD)."
    echo "Syncing .venv/.git/SQLite causes lock errors. Move the project and re-run."
    read -r -p "Continue here anyway? (y/N) " answer
    [[ "$answer" =~ ^[Yy]$ ]] || exit 1
    ;;
esac

step "Checking for uv (Python package manager)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv --version

step "Installing Python 3.13 (if needed) and dependencies"
uv sync

step "Preparing .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env. Fill in TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS and LLM_API_KEY."
  "${EDITOR:-nano}" .env || true
else
  echo ".env already exists; leaving it untouched."
fi

step "Finding your Telegram user ID"
if grep -Eq '^ALLOWED_USER_IDS=[[:space:]]*$' .env; then
  # whoami prints your bot's name and waits up to 2 minutes for your /start.
  uv run python -m jobpilot whoami --save || true
else
  echo "ALLOWED_USER_IDS is already set."
fi

step "Running doctor"
if uv run python -m jobpilot doctor; then
  printf '\n\033[32mAll set. Start the bot with:  uv run python -m jobpilot run\033[0m\n'
else
  echo "Fix the FAIL rows (see docs/SETUP.md), then run: uv run python -m jobpilot doctor"
  exit 1
fi
