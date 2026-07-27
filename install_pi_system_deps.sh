#!/usr/bin/env bash
set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This script is for Raspberry Pi OS / Debian systems with apt-get." >&2
    exit 1
fi

sudo apt-get update
sudo apt-get install -y python3-lgpio alsa-utils ffmpeg

python3 - <<'PY'
import lgpio
print("lgpio OK:", getattr(lgpio, "__file__", "built-in"))
PY
