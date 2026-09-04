#!/bin/bash
# ─────────────────────────────────────────────
# AAT Bridge — Start
# ─────────────────────────────────────────────
DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv/bin/python3"

if [ ! -f "$VENV" ]; then
    echo "❌ Virtual Environment nicht gefunden."
    echo "   Bitte zuerst: ./install.sh"
    exit 1
fi

if [ "$1" == "--setup" ]; then
    exec "$VENV" "$DIR/setup.py"
elif [ "$1" == "--config" ]; then
    exec "$VENV" "$DIR/setup.py" --config
elif [ "$1" == "--reinstall-service" ]; then
    exec "$VENV" "$DIR/setup.py" --reinstall-service
else
    exec "$VENV" "$DIR/ac_bridge.py"
fi
