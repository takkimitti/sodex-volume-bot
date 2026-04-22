#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "  SoDEX Bot v2.0 - Pro Combat Edition"
echo "=========================================="

if [ -f .env ]; then
    echo "[INFO] .env ファイルを読み込み中..."
    set -a
    source .env
    set +a
fi

echo "[INFO] Bot起動..."
python3 -u sodex_bot_v2.py
