#!/usr/bin/env bash
# GCP startup script — runs as root on first boot.
# Clones the repo, fetches secrets, writes .env, installs systemd service.
set -euo pipefail

GITHUB_REPO="git@github.com:bigjmn/kalshi-bot.git"
INSTALL_DIR="/opt/kalshi-btc"
SERVICE_USER="kalshi"
KEY_PATH="/etc/kalshi/private_key.pem"

# ── system deps ──────────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -qq git curl

# ── uv ───────────────────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="/root/.local/bin:$PATH"
fi
export PATH="/root/.local/bin:$PATH"

# ── github ssh key ───────────────────────────────────────────────────────────
mkdir -p /root/.ssh /etc/kalshi
gcloud secrets versions access latest --secret="github-deploy-key" > /root/.ssh/github_deploy_key
chmod 600 /root/.ssh/github_deploy_key
gcloud secrets versions access latest --secret="firebase-credentials" > /etc/kalshi/firebase-credentials.json
chmod 600 /etc/kalshi/firebase-credentials.json
ssh-keyscan github.com >> /root/.ssh/known_hosts
cat >> /root/.ssh/config <<EOF
Host github.com
  IdentityFile /root/.ssh/github_deploy_key
  StrictHostKeyChecking yes
EOF

# ── clone repo ───────────────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR" ]; then
    git -C "$INSTALL_DIR" pull
else
    git clone "$GITHUB_REPO" "$INSTALL_DIR"
fi

# ── private key from Secret Manager ─────────────────────────────────────────
mkdir -p /etc/kalshi
gcloud secrets versions access latest --secret="kalshi-private-key" > "$KEY_PATH"
chmod 600 "$KEY_PATH"

# ── .env ─────────────────────────────────────────────────────────────────────
KALSHI_KEY_ID=$(gcloud secrets versions access latest --secret="kalshi-key-id")

cat > "$INSTALL_DIR/.env" <<EOF
KALSHI_KEY_ID=$KALSHI_KEY_ID
KALSHI_PRIVATE_KEY_PATH=$KEY_PATH
KALSHI_ENV=prod
KALSHI_OUTPUT_DIR=$INSTALL_DIR/data
KALSHI_SNAPSHOT_INTERVAL_SEC=1.0
KALSHI_REST_SEED=True
KALSHI_DISCOVERY_LOOKAHEAD_MIN=900
KALSHI_KELLY_FRACTION=1.0
FIREBASE_CREDENTIALS_PATH=/etc/kalshi/firebase-credentials.json
FIREBASE_PROJECT_ID=kalshi-bot-494522
EOF
chmod 600 "$INSTALL_DIR/.env"

# ── sync python deps ─────────────────────────────────────────────────────────
cd "$INSTALL_DIR"
uv sync

# ── systemd service ──────────────────────────────────────────────────────────
UV_BIN="$(command -v uv)"

cat > /etc/systemd/system/kalshi-bot.service <<EOF
[Unit]
Description=Kalshi BTC trading bot
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$UV_BIN run python main.py
Restart=on-failure
RestartSec=10
EnvironmentFile=$INSTALL_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable kalshi-bot
systemctl start kalshi-bot
