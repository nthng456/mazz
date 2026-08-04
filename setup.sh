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
    "rich>=13.7.0"
echo "  ✓ playwright, requests, pycryptodome, huggingface_hub, rich installed"

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
echo "========================================"
echo ""

# --- Run the downloader (detached / background) ---
# The downloader is launched with `setsid nohup ... &` so it keeps running
# after the terminal/SSH session is closed (e.g. when you disconnect from the
# Codespace). Output is redirected to a log file; the PID is saved so you can
# check on or stop the run later.
#
# NOTE: this only survives a *disconnect*. GitHub Codespaces still stops the
# whole container after its idle timeout — raise it under
# Settings → Codespaces → "Default idle timeout" (or keep the Codespace open).

LOG_FILE="download.log"
PID_FILE="download.pid"

# Inputs may come from the environment (works in non-interactive/CI shells):
#   JSON_PATH=data.json OUTPUT_DIR=./data ./setup.sh
json_path="${JSON_PATH:-}"
output_dir="${OUTPUT_DIR:-}"

if [ -z "$json_path" ] || [ -z "$output_dir" ]; then
    if [ ! -t 0 ]; then
        echo "Non-interactive shell and JSON_PATH/OUTPUT_DIR not set — skipping run."
        echo "Run it yourself in the background with:"
        echo "  JSON_PATH=<json> OUTPUT_DIR=./data ./setup.sh"
        echo "or:"
        echo "  setsid nohup python biomaze_downloader.py <json_path> ./data > $LOG_FILE 2>&1 &"
        exit 0
    fi

    # Suggest a JSON from the current directory as the default (first match).
    default_json=""
    for f in *.json; do
        [ -e "$f" ] && default_json="$f" && break
    done

    while :; do
        if [ -n "$default_json" ]; then
            read -r -p "Path to the JSON file [${default_json}]: " json_path || exit 0
            json_path="${json_path:-$default_json}"
        else
            read -r -p "Path to the JSON file: " json_path || exit 0
        fi

        if [ -z "$json_path" ]; then
            echo "  Please enter a path."
            continue
        fi
        if [ ! -f "$json_path" ]; then
            echo "  ✗ File not found: $json_path — try again."
            continue
        fi
        break
    done

    read -r -p "Output directory [./data]: " output_dir || exit 0
    output_dir="${output_dir:-./data}"
fi

if [ ! -f "$json_path" ]; then
    echo "  ✗ File not found: $json_path"
    exit 1
fi

echo ""
echo "========================================"
echo " Launching downloader in the background"
echo "   JSON:   $json_path"
echo "   Output: $output_dir"
echo "   Log:    $LOG_FILE"
echo "========================================"
echo ""

# setsid detaches from the controlling terminal; nohup ignores SIGHUP so the
# process is not killed when the terminal/SSH session closes.
setsid nohup python biomaze_downloader.py "$json_path" "$output_dir" \
    > "$LOG_FILE" 2>&1 &
child_pid=$!
echo "$child_pid" > "$PID_FILE"

echo "  ✓ Started (PID $child_pid). It will keep running after you disconnect."
echo ""
echo "  Follow progress:   tail -f $LOG_FILE"
echo "  Check if running:  kill -0 $child_pid 2>/dev/null && echo running || echo stopped"
echo "  Stop it:           kill \$(cat $PID_FILE)"
