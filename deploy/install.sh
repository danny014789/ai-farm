#!/bin/bash
# Install plant-ops-ai as a systemd service.
# Run from the project root: bash deploy/install.sh

set -e

SERVICE_NAME="plant-ops-ai"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CURRENT_USER="$(whoami)"
HOME_DIR="$(eval echo ~$CURRENT_USER)"

echo "Installing $SERVICE_NAME service..."
echo "  User: $CURRENT_USER"
echo "  Home: $HOME_DIR"
echo "  Project: $PROJECT_DIR"
echo ""

# Generate service file with current user's paths
sed -e "s|__USER__|$CURRENT_USER|g" \
    -e "s|__HOME__|$HOME_DIR|g" \
    "$SCRIPT_DIR/plant-ops-ai.service" \
    > /tmp/plant-ops-ai.service

# Install
sudo cp /tmp/plant-ops-ai.service /etc/systemd/system/plant-ops-ai.service
rm /tmp/plant-ops-ai.service

sudo systemctl daemon-reload
sudo systemctl enable plant-ops-ai

echo ""
echo "Service installed and enabled (will start on boot)."
echo ""
echo "Commands:"
echo "  sudo systemctl start plant-ops-ai     # Start now"
echo "  sudo systemctl stop plant-ops-ai      # Stop"
echo "  sudo systemctl restart plant-ops-ai   # Restart (after git pull)"
echo "  sudo systemctl status plant-ops-ai    # Check status"
echo "  sudo journalctl -u plant-ops-ai -f    # Live logs"
