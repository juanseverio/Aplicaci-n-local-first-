#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
python3 server.py --port "${PORT:-8765}"
