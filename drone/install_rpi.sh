#!/usr/bin/env bash
# Run on the Raspberry Pi 5 (Raspberry Pi OS Bookworm 64-bit) from the repo root:  ./drone/install_rpi.sh
# Idempotent. Installs deps, enables the GPIO UART for the flight controller, installs the systemd service.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
sudo apt-get update
sudo apt-get install -y python3-venv python3-picamera2 python3-opencv git
[ -d .venv ] || python3 -m venv --system-site-packages .venv
.venv/bin/pip install -q -r drone/requirements.txt

# GPIO14/15 UART for the flight controller (Pi 5: uart0 on the 40-pin header -> /dev/ttyAMA0, /dev/serial0 symlink)
CFG=/boot/firmware/config.txt
grep -q "^enable_uart=1" $CFG || echo "enable_uart=1" | sudo tee -a $CFG
grep -q "^dtparam=uart0=on" $CFG || echo "dtparam=uart0=on" | sudo tee -a $CFG
# make sure the serial console is not using that UART
sudo sed -i 's/console=serial0,115200 //; s/console=ttyAMA0,115200 //' /boot/firmware/cmdline.txt
sudo usermod -aG dialout,video "$USER"

# systemd unit with this user and this checkout path
sed -e "s|User=pi|User=$USER|" -e "s|/home/pi/vayuveer-cloud-gcs|$ROOT|g" drone/vayuveer-agent.service | sudo tee /etc/systemd/system/vayuveer-agent.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable vayuveer-agent
echo
echo "Installed. Now edit drone/config.yaml (server_url, token, drone.mode: mavlink, camera.source), reboot once, then:"
echo "  sudo systemctl start vayuveer-agent && journalctl -fu vayuveer-agent"
