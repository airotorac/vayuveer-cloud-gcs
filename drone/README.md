# VayuVeer Cloud GCS — Raspberry Pi 5 setup

The Pi 5 rides on the aircraft, talks MAVLink to the flight controller, streams video, and keeps an
outbound link to the relay at `wss://gcs.vayuveer.in`. Once installed it starts on every boot: in the
field the Pi only needs power and internet (Wi-Fi or a 4G/5G USB dongle).

## Quick install (double-click)

Copy `VayuVeer-RPi-Installer.sh` (from this folder, or the pre-filled copy you were given) onto the Pi
desktop, double-click it, choose **Execute in Terminal**, and answer the questions (access token,
aircraft ID, camera type). It clones this repository, installs everything, writes the config, installs
the auto-start service and offers to reboot.

The manual steps below do the same thing.

## 1. Wiring

Connect the flight controller's **TELEM2** port to the Pi's 40-pin header. Both sides are 3.3 V logic;
no level shifter is needed.

| FC TELEM2 | Pi 5 header |
|---|---|
| TX | GPIO15 RXD — physical pin 10 |
| RX | GPIO14 TXD — physical pin 8 |
| GND | GND — physical pin 6 |

Alternative: plug the FC's USB into the Pi and use `/dev/ttyACM0` (the installer auto-detects this).
Power the Pi 5 from a 5 V / 5 A BEC, not from the flight controller.

## 2. Flight controller parameters (ArduPilot Copter)

| Parameter | Value | Why |
|---|---|---|
| `SERIAL2_PROTOCOL` | 2 | MAVLink 2 on TELEM2 |
| `SERIAL2_BAUD` | 921 | 921600 baud |
| `FS_GCS_ENABLE` | 1 | RTL if the Pi (which acts as the GCS) stops sending heartbeats |
| `GUID_TIMEOUT` | 3 (default) | stops joystick velocity if the Pi stops updating it |

## 3. Install

On Raspberry Pi OS Bookworm (64-bit), with internet:

```bash
git clone https://github.com/airotorac/vayuveer-cloud-gcs.git ~/vayuveer-cloud-gcs
cd ~/vayuveer-cloud-gcs && ./drone/install_rpi.sh
```

## 4. Configure

`nano drone/config.yaml` — change these lines (the token is `VAYUVEER_TOKEN` from the relay's
`deploy/.env`; ask the administrator):

```yaml
server_url: wss://gcs.vayuveer.in
token: <access token>
drone_id: vayuveer-01

drone:
  mode: mavlink
  connection: /dev/serial0      # /dev/ttyACM0 if the FC is on USB
  baud: 921600

camera:
  source: picamera2             # CSI camera. opencv = USB camera or an rtsp:// gimbal stream
  width: 1280
  height: 720
  fps: 10
```

## 5. Reboot, start, watch

```bash
sudo reboot
```
```bash
sudo systemctl start vayuveer-agent && journalctl -fu vayuveer-agent
```

Expect `heartbeat from system 1` (FC link) and `relay connected` (cloud link). The service is enabled,
so it restarts on every boot and after any crash.

## 6. First flight test

1. **Props off.** Sign in at https://gcs.vayuveer.in, select the aircraft in the header dropdown.
   Confirm the FC pill is green, telemetry matches the flight controller, and video is flowing.
   Arm and Disarm from the dashboard.
2. **Props on, open area.** Arm → Take off to 5 m → Hold → Land. Try the joysticks gently.
3. **Failsafe check** at low altitude: pull the internet dongle. After 10 s without the cloud link the
   aircraft returns home on its own (`failsafe.link_loss_s` / `failsafe.action` in `config.yaml`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `heartbeat lost` / no FC pill | Check TX/RX are crossed, `SERIAL2_*` params, `ls -l /dev/serial0`. |
| `camera 'picamera2' failed` | Run `rpicam-hello` to test the camera; or set `camera.source: opencv`. The agent falls back to a simulated feed and keeps flight functions working. |
| `relay link down` | The Pi has no internet or the token is wrong (relay closes with code 4401). |
| Video stutters on 4G | Lower `camera.fps` / `camera.width` in `config.yaml`. |
| Update the software | `cd ~/vayuveer-cloud-gcs && git pull && sudo systemctl restart vayuveer-agent` |
