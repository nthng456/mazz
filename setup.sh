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
echo "[1/4] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg curl > /dev/null 2>&1
echo "  ✓ ffmpeg installed"

# --- Python deps ---
# huggingface_hub must be >=1.0: bucket support (sync_bucket,
# get_bucket_paths_info) does not exist in the 0.x series.
echo "[2/4] Installing Python packages..."
pip install --quiet \
    "playwright>=1.61.0" \
    "requests>=2.31.0" \
    "pycryptodome>=3.20.0" \
    "huggingface_hub>=1.26.0" \
    "hydrogram>=0.2.0"
echo "  ✓ playwright, requests, pycryptodome, huggingface_hub, hydrogram installed"

# TgCrypto is a C extension that Hydrogram picks up automatically when present
# and does the MTProto AES in native code instead of pure Python. It is an
# optimisation, not a requirement, and it has no wheels for the newest Python
# releases — so a failed build downgrades to a warning rather than killing setup.
if pip install --quiet "tgcrypto>=1.2.5" 2>/dev/null; then
    echo "  ✓ tgcrypto installed (native MTProto crypto)"
else
    echo "  ⚠  tgcrypto unavailable for this Python — Hydrogram falls back to pure Python (slower)"
fi

python - <<'PY'
import sys
from huggingface_hub import __version__ as v
try:
    from huggingface_hub import get_bucket_paths_info, sync_bucket  # noqa: F401
except ImportError:
    sys.exit(f"  ✗ huggingface_hub {v} lacks bucket support — need >=1.0")
print(f"  ✓ huggingface_hub {v} with bucket support")
PY

# --- Playwright browsers ---
echo "[3/4] Installing Chromium for Playwright..."
playwright install --with-deps chromium
echo "  ✓ Chromium installed"

# --- HF auth ---
# The downloader calls the huggingface_hub library directly, so a stored
# login or HF_TOKEN in the environment is all that is needed.
echo "[4/4] Checking Hugging Face authentication..."
if python -c "
import sys
from huggingface_hub import whoami
try:
    print('  ✓ Authenticated as', whoami()['name'])
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    :
else
    echo "  ⚠  Not authenticated. Set a write token before running:"
    echo "       export HF_TOKEN=hf_xxxxx"
    echo "     or log in interactively:"
    echo "       hf auth login"
fi

echo ""
echo "========================================"
echo " Setup complete!"
echo ""
echo " Telegram session (once, interactive):"
echo "   export TG_API_ID=1234567"
echo "   export TG_API_HASH=0123456789abcdef0123456789abcdef"
echo "   python make_session.py"
echo ""
echo " Run:  python biomaze_downloader.py input.json ./data"
echo "========================================"
