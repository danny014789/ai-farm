#!/bin/bash
# Install the plant-ops-ai watchdog as a root cron job and configure the
# sudoers rule needed for the /restart Telegram command.
#
# Run from the project root:
#   sudo bash deploy/install_watchdog.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
WATCHDOG="$SCRIPT_DIR/watchdog.sh"
CURRENT_USER="${SUDO_USER:-$(logname 2>/dev/null || whoami)}"
SUDOERS_FILE="/etc/sudoers.d/plant-ops-ai"
CRON_MARKER="plant-ops-ai-watchdog"

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: this script must be run with sudo." >&2
    exit 1
fi

echo "Installing plant-ops-ai watchdog..."
echo "  User:    $CURRENT_USER"
echo "  Project: $PROJECT_DIR"
echo "  Watchdog: $WATCHDOG"
echo ""

# Make watchdog executable
chmod +x "$WATCHDOG"

# --- sudoers rule for /restart Telegram command ---------------------------
echo "Configuring sudoers: $CURRENT_USER can restart plant-ops-ai without password..."
cat > "$SUDOERS_FILE" <<EOF
# Allow the plant-ops-ai service user to restart itself via the /restart
# Telegram command without a password prompt.
$CURRENT_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart plant-ops-ai
EOF
chmod 440 "$SUDOERS_FILE"
echo "  Written: $SUDOERS_FILE"

# --- Root cron job for watchdog -------------------------------------------
echo "Installing root cron job (every 5 minutes)..."
# Remove any existing entry for this watchdog, then add a new one
( crontab -l 2>/dev/null | grep -v "$CRON_MARKER" || true
  echo "*/5 * * * * $WATCHDOG  # $CRON_MARKER"
) | crontab -
echo "  Cron entry added."

echo ""
echo "Done. The watchdog will:"
echo "  - Run every 5 minutes"
echo "  - Restart plant-ops-ai if the service is not active"
echo "  - Restart plant-ops-ai if the heartbeat file is stale (>10 min)"
echo ""
echo "Telegram /restart command is also now enabled for user: $CURRENT_USER"
