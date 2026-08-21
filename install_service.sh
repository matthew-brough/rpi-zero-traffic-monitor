#!/bin/bash
# Install systemd service for gmap_tracker

set -e

SERVICE_NAME="gmap_tracker"
SERVICE_FILE="gmap_tracker.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing ${SERVICE_NAME} service..."

# Copy service file to systemd directory
sudo cp "${SCRIPT_DIR}/${SERVICE_FILE}" /etc/systemd/system/

# Reload systemd daemon
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable ${SERVICE_NAME}

# Start the service
sudo systemctl start ${SERVICE_NAME}

echo ""
echo "✓ Service installed and started!"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status ${SERVICE_NAME}   # Check status"
echo "  sudo systemctl stop ${SERVICE_NAME}     # Stop service"
echo "  sudo systemctl start ${SERVICE_NAME}    # Start service"
echo "  sudo systemctl restart ${SERVICE_NAME}  # Restart service"
echo "  sudo journalctl -u ${SERVICE_NAME} -f   # View live logs"
echo "  sudo journalctl -u ${SERVICE_NAME} -n 50  # View last 50 log lines"
