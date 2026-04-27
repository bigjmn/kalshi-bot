#!/usr/bin/env bash
set -euo pipefail

# ── install uv if not present ────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# ── sync dependencies ────────────────────────────────────────────────────────
uv sync

# ── check .env exists ────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo "ERROR: .env not found. Create it with:"
    echo "  KALSHI_KEY_ID=your_key_id"
    echo "  KALSHI_PRIVATE_KEY_PATH=/path/to/private_key.pem"
    echo "  KALSHI_ENV=prod"
    echo "  KALSHI_KELLY_FRACTION=0.25"
    exit 1
fi

# ── install systemd service (Linux only) ─────────────────────────────────────
if [[ "$(uname)" == "Linux" ]] && command -v systemctl &>/dev/null; then
    SERVICE_FILE="/etc/systemd/system/kalshi-bot.service"
    WORKING_DIR="$(pwd)"
    UV_BIN="$(command -v uv)"
    USER_NAME="$(whoami)"

    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Kalshi BTC trading bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$WORKING_DIR
ExecStart=$UV_BIN run python main.py
Restart=on-failure
RestartSec=10
EnvironmentFile=$WORKING_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable kalshi-bot
    echo ""
    echo "Service installed. Commands:"
    echo "  sudo systemctl start kalshi-bot     # start"
    echo "  sudo systemctl stop kalshi-bot      # stop"
    echo "  journalctl -u kalshi-bot -f         # tail logs"
else
    echo ""
    echo "To run directly:"
    echo "  uv run python main.py"
fi

echo ""
echo "Setup complete."
