#!/bin/bash
# ============================================================
# setup.sh — One-command setup for GitHub Codespace / Ubuntu
# Usage:  chmod +x setup.sh && ./setup.sh
# ============================================================

set -e

echo "========================================"
echo " Biomaze Downloader — Setup"
echo "========================================"

# --- System packages ---
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg curl > /dev/null 2>&1
echo "  ✓ ffmpeg installed"

# --- Python deps ---
echo "[2/5] Installing Python packages..."
pip install --quiet playwright requests pycryptodome
echo "  ✓ playwright, requests, pycryptodome installed"

# --- Playwright browsers ---
echo "[3/5] Installing Chromium for Playwright..."
playwright install --with-deps chromium
echo "  ✓ Chromium installed"

# --- Hugging Face CLI ---
echo "[4/5] Installing Hugging Face CLI..."
curl -fsSL https://hf.co/cli/install.sh | bash
echo "  ✓ hf CLI installed"

# --- HF Login ---
echo "[5/5] Logging in to Hugging Face..."
if [ -n "$HF_TOKEN" ]; then
    hf auth login --token "$HF_TOKEN"
    echo "  ✓ Logged in via HF_TOKEN"
else
    echo "  ⚠  HF_TOKEN not set. Run manually:"
    echo "     export HF_TOKEN=hf_xxxxx"
    echo "     hf auth login --token \$HF_TOKEN"
fi

echo ""
echo "========================================"
echo " Setup complete!"
echo " Run:  python biomaze_downloader.py input.json"
echo "========================================"
