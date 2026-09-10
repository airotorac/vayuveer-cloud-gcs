#!/usr/bin/env bash
# Run on the Raspberry Pi 5 (Raspberry Pi OS Bookworm 64-bit). Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."
sudo apt-get update
sudo apt-get install -y python3-venv python3-picamera2 python3-opencv
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r drone/requirements.txt
# Enable the UART that talks to the flight controller (TELEM2 <-> GPIO14/15)
if ! grep -q "^enable_uart=1" /boot/firmware/config.txt; then
  echo "enable_uart=1" | sudo tee -a /boot/firmware/config.txt
  echo "dtoverlay=disable-bt" | sudo tee -a /boot/firmware/config.txt
fi
sudo usermod -aG dialout,video "$USER"
sudo cp drone/vayuveer-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vayuveer-agent
echo "Edit drone/config.yaml (server_url, token, drone.mode: mavlink, camera.source), then: sudo systemctl start vayuveer-agent"
