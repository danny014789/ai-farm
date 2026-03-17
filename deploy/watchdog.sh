#!/bin/bash
# Watchdog for plant-ops-ai.
#
# Restarts the service if:
#   1. The systemd service is not active (crashed / failed), OR
#   2. The heartbeat file has not been updated in MAX_AGE seconds
#      (bot process is hung — running but not processing the job queue).
#
# Install (run once on the Pi):
#   sudo bash deploy/install_watchdog.sh
#
# Or add manually to root crontab:
#   sudo crontab -e
#   */5 * * * * /home/pi/plant-ops-ai/deploy/watchdog.sh

set -euo pipefail

SERVICE="plant-ops-ai"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$PROJECT_DIR/data"
HEARTBEAT="$DATA_DIR/.heartbeat"
LOG="$DATA_DIR/watchdog.log"
MAX_AGE=600  # 10 minutes — heartbeat is written every 5 min

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] $*" | tee -a "$LOG"
}

# Rotate log file if it exceeds 1 MB
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    mv "$LOG" "${LOG}.old"
fi

# --- Check 1: service not active ------------------------------------------
if ! systemctl is-active --quiet "$SERVICE"; then
    log "Service $SERVICE is not active. Restarting..."
    systemctl restart "$SERVICE"
    log "Restart triggered."
    exit 0
fi

# --- Check 2: heartbeat stale ---------------------------------------------
if [ ! -f "$HEARTBEAT" ]; then
    log "No heartbeat file found at $HEARTBEAT. Skipping stale-check."
    exit 0
fi

# Get file modification time in epoch seconds
if command -v stat &>/dev/null; then
    FILE_TIME=$(stat -c %Y "$HEARTBEAT" 2>/dev/null || echo 0)
else
    FILE_TIME=0
fi

NOW=$(date +%s)
AGE=$(( NOW - FILE_TIME ))

if [ "$AGE" -gt "$MAX_AGE" ]; then
    log "Heartbeat stale (${AGE}s old, max allowed ${MAX_AGE}s). Bot may be hung. Restarting $SERVICE..."
    systemctl restart "$SERVICE"
    log "Restart triggered."
else
    : # Bot is healthy — no action needed
fi
