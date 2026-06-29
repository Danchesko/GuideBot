#!/bin/bash
# Setup and start GuideBot as a systemd service

SERVICE_FILE="/etc/systemd/system/guidebot.service"
WORK_DIR="/home/ubuntu/dev/GuideBot"

echo "Setting up GuideBot systemd service..."

# Stop existing service if running
sudo systemctl stop guidebot 2>/dev/null

# Create service file
sudo cp "$WORK_DIR/guidebot.service" "$SERVICE_FILE"

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable guidebot
sudo systemctl start guidebot

# Verify
sleep 2
sudo systemctl status guidebot
