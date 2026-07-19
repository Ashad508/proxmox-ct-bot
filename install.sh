#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then
    printf 'Error: run install.sh as root on the Proxmox VE host.\n' >&2
    exit 1
fi
for tool in pct pvesh pvesm pveum; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Error: %s is missing. Install this bot directly on a Proxmox VE host.\n' "$tool" >&2
        exit 1
    fi
done
mkdir -p data logs
chmod 700 data logs
chmod 600 .env
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

printf '\nInstalled. Edit .env, then validate as root:\n  .venv/bin/python bot.py --check\n'
