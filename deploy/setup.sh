#!/usr/bin/env bash
# AIPM Server Setup Script — run this on a fresh Ubuntu 22.04/24.04 VPS
# Usage: curl -sSL <raw-url> | bash   OR   bash deploy/setup.sh
set -euo pipefail

echo "=== AIPM Server Setup ==="
echo ""

# --- 1. System packages ---
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq git curl unzip python3 python3-pip python3-venv \
    build-essential sqlite3 ca-certificates gnupg

# --- 2. Node.js 22 (for Claude Code CLI) ---
echo "[2/7] Installing Node.js 22..."
if ! command -v node &>/dev/null || [[ "$(node -v)" != v22* ]]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo apt-get install -y -qq nodejs
fi
echo "  Node.js $(node -v)"

# --- 3. Claude Code CLI ---
echo "[3/7] Installing Claude Code CLI..."
if ! command -v claude &>/dev/null; then
    sudo npm install -g @anthropic-ai/claude-code
fi
echo "  Claude CLI installed: $(claude --version 2>/dev/null || echo 'installed')"

# --- 4. Caddy (reverse proxy for HTTPS) ---
echo "[4/7] Installing Caddy..."
if ! command -v caddy &>/dev/null; then
    sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq caddy
fi

# --- 5. Create aipm user ---
echo "[5/7] Setting up aipm user..."
if ! id aipm &>/dev/null; then
    sudo useradd -m -s /bin/bash aipm
fi
sudo -u aipm mkdir -p /home/aipm/.config

# --- 6. Clone AIPM ---
echo "[6/7] Setting up AIPM..."
AIPM_DIR="/opt/aipm"
if [ ! -d "$AIPM_DIR" ]; then
    sudo mkdir -p "$AIPM_DIR"
    sudo chown aipm:aipm "$AIPM_DIR"
fi

# --- 7. Python venv + install ---
echo "[7/7] Creating Python environment..."
if [ ! -d "$AIPM_DIR/.venv" ]; then
    sudo -u aipm python3 -m venv "$AIPM_DIR/.venv"
fi

echo ""
echo "=== Base setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Clone your repo:     sudo -u aipm git clone <your-repo-url> $AIPM_DIR/app"
echo "  2. Install AIPM:        sudo -u aipm $AIPM_DIR/.venv/bin/pip install -e $AIPM_DIR/app"
echo "  3. Configure:           sudo -u aipm cp $AIPM_DIR/app/config.example.toml $AIPM_DIR/config.toml"
echo "  4. Set secrets:         sudo vim /opt/aipm/.env"
echo "  5. Login Claude CLI:    sudo -u aipm claude login"
echo "  6. Install services:    sudo cp $AIPM_DIR/app/deploy/aipm.service /etc/systemd/system/"
echo "  7. Start:               sudo systemctl enable --now aipm"
