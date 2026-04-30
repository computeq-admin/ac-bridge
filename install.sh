#!/bin/bash
# ─────────────────────────────────────────────
# AAT Bridge — Installation
# ─────────────────────────────────────────────
set -e

echo ""
echo "========================================"
echo "  AAT Bridge — AI Agent Tasks"
echo "  Installation"
echo "========================================"
echo ""

# Python3 vorhanden?
if ! command -v python3 &> /dev/null; then
    echo "❌ python3 nicht gefunden. Bitte installieren:"
    echo "   sudo apt install python3 python3-venv"
    exit 1
fi

# python3-venv vorhanden?
if ! python3 -c "import venv" &> /dev/null; then
    echo "❌ python3-venv nicht gefunden. Bitte installieren:"
    echo "   sudo apt install python3-venv"
    exit 1
fi

# Virtual environment erstellen
echo "→ Erstelle Virtual Environment..."
python3 -m venv venv
echo "✓ Virtual Environment erstellt"

# pip updaten
echo "→ Aktualisiere pip..."
venv/bin/pip install --upgrade pip -q
echo "✓ pip aktualisiert"

# Dependencies installieren
echo "→ Installiere Dependencies..."
venv/bin/pip install -r requirements.txt -q
echo "✓ Dependencies installiert"

# start.sh ausführbar machen
chmod +x start.sh

echo ""
echo "========================================"
echo "  Installation abgeschlossen!"
echo "========================================"
echo ""
echo "  Weiter mit der Einrichtung:"
echo "  ./start.sh --setup"
echo ""
