#!/usr/bin/env bash

# Raspberry Pi bootstrap for this project.
#
# Recommended:
#   source ./setup_pi.sh
#
# Running it directly also installs/updates everything, but activation will not
# persist in your current terminal after the script exits.

if [ -n "${BASH_SOURCE:-}" ]; then
    _setup_pi_path="${BASH_SOURCE[0]}"
else
    _setup_pi_path="$0"
fi

_setup_pi_dir="$(CDPATH= cd -- "$(dirname -- "$_setup_pi_path")" && pwd)" || return 1 2>/dev/null || exit 1
_setup_pi_sourced=0

if [ -n "${BASH_SOURCE:-}" ] && [ "${BASH_SOURCE[0]}" != "$0" ]; then
    _setup_pi_sourced=1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "setup_pi.sh: this script is for Raspberry Pi OS / Debian systems with apt-get." >&2
    return 1 2>/dev/null || exit 1
fi

if command -v deactivate >/dev/null 2>&1; then
    deactivate 2>/dev/null || true
fi

hash -r 2>/dev/null || true

sudo apt-get update || return 1 2>/dev/null || exit 1
sudo apt-get install -y python3-venv python3-lgpio alsa-utils ffmpeg || return 1 2>/dev/null || exit 1

/usr/bin/python3 - <<'PY' || return 1 2>/dev/null || exit 1
import lgpio
print("System lgpio OK:", getattr(lgpio, "__file__", "built-in"))
PY

if [ -f "$_setup_pi_dir/.venv/pyvenv.cfg" ] && ! grep -q "^include-system-site-packages = true" "$_setup_pi_dir/.venv/pyvenv.cfg"; then
    echo "Recreating .venv with system site packages enabled"
    rm -rf "$_setup_pi_dir/.venv"
fi

. "$_setup_pi_dir/setup_venv.sh" || return 1 2>/dev/null || exit 1

python - <<'PY' || return 1 2>/dev/null || exit 1
import flask
import lgpio
import requests
import zeroconf

print("Project Python OK")
print("lgpio:", getattr(lgpio, "__file__", "built-in"))
PY

if [ "$_setup_pi_sourced" != "1" ]; then
    echo "Run 'source ./setup_pi.sh' to keep .venv active in this terminal."
fi

unset _setup_pi_path
unset _setup_pi_dir
unset _setup_pi_sourced
