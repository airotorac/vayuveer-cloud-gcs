# VayuVeer Cloud GCS

VayuVeer Cloud GCS is the ground control station for the AiRotor AX4 (or any ArduPilot/PX4 aircraft) that works
over the **internet** instead of a line-of-sight radio: a Raspberry Pi 5 on the aircraft
dials out to a small relay server over 4G/5G or Wi-Fi, and an installable web app gives
the operator live video, a moving map, telemetry, flight buttons, virtual joysticks and
mission planning from any browser, phone or tablet.

```
┌────────────────────── aircraft ──────────────────────┐        ┌── cloud (VPS) ──┐       ┌── operator ──┐
│ Flight controller ◄─MAVLink UART─► Raspberry Pi 5    │        │                 │       │              │
│ (ArduPilot Copter)                  drone/agent.py   │─WSS──► │  server/main.py │ ◄─WSS─│  dashboard/  │
│ Camera / gimbal  ─────────────────► JPEG frames      │  4G    │  relay + auth   │       │  PWA         │
└──────────────────────────────────────────────────────┘        └─────────────────┘       └──────────────┘
```

Both ends connect **outbound**, so neither the drone nor the operator needs a public IP,
port-forwarding or a VPN. One shared token authenticates both sides.

## What is in the box

| Path | Runs on | Purpose |
|---|---|---|
| `drone/` | Raspberry Pi 5 | Agent: MAVLink bridge, camera streamer, joystick watchdog, link-loss failsafe. Has a **mock** mode that simulates a drone so the whole stack runs on a laptop. |
| `server/` | Any VPS / Docker | Relay: WebSocket fan-out, token auth, serves the dashboard. |
| `dashboard/` | Browser | The app: video + HUD, Leaflet map, telemetry, ARM/TAKEOFF/LAND/RTL, dual virtual joysticks (or WASD/QE/RF keys), waypoint missions, gimbal, EO/IR toggle. Installable as a PWA. |
| `deploy/` | VPS | `docker compose` with Caddy for automatic HTTPS. |

## Live deployment

The production relay runs at **https://gcs.vayuveer.in** (AWS EC2, Mumbai). Point a Pi at it with
`server_url: wss://gcs.vayuveer.in` in `drone/config.yaml` and the shared token from `deploy/.env`.

## 1. Try it on your laptop (no hardware)

```bash
cd vayuveer-cloud-gcs
./run_local.sh                      # relay on :8000 + simulated drone
```
Open <http://127.0.0.1:8000/?token=dev-token>. ARM → CONFIRM ARM → TAKEOFF, then
right-click the map to fly somewhere, enable PLAN and tap to build a mission, UPLOAD, START.
Keyboard: `W/S` pitch, `A/D` roll, `Q/E` yaw, `R/F` climb/descend, `Space` = stop & LOITER.

## 2. Deploy the relay (once)

On a VPS with a DNS name pointing at it:

```bash
cd deploy
cp .env.example .env               # set DOMAIN and a long random VAYUVEER_TOKEN
docker compose up -d --build
```
Caddy obtains a Let's Encrypt certificate automatically. The dashboard is now at
`https://<DOMAIN>/` and the drone endpoint is `wss://<DOMAIN>/ws/drone/<id>`.

Bandwidth: telemetry is ~3 kB/s; video at 640×360, 10 fps, JPEG q65 is roughly 300–500 kbit/s.
Raise `camera.width/height/fps/quality` in `config.yaml` if the 4G link allows.

## 3. Set up the Raspberry Pi 5 on the aircraft

Wiring: FC `TELEM2` TX/RX/GND ↔ Pi GPIO15 (RXD) / GPIO14 (TXD) / GND, or plug the FC's USB
into the Pi and use `/dev/ttyACM0`. Set on the FC: `SERIAL2_PROTOCOL=2` (MAVLink2),
`SERIAL2_BAUD=921` and, so the Pi can drive GUIDED velocity, `SYSID_MYGCS=255` is fine (agent sends as sysid 255).

```bash
git clone <this repo> ~/vayuveer-cloud-gcs && cd ~/vayuveer-cloud-gcs
./drone/install_rpi.sh
nano drone/config.yaml       # server_url: wss://<DOMAIN>, token, drone_id, drone.mode: mavlink, camera.source: picamera2|opencv
sudo reboot                  # UART overlay takes effect
sudo systemctl start vayuveer-agent && journalctl -fu vayuveer-agent
```

Camera options (`camera.source`): `picamera2` for a CSI camera module, `opencv` for a USB
camera / HDMI capture dongle / an `rtsp://` URL from the gimbal, `mock` for a synthetic feed.

## Safety features built in

* **Arm confirmation** – ARM must be pressed twice within 4 s; the agent also rejects arm
  commands without the confirm flag.
* **Joystick watchdog** – velocity setpoints are re-sent at 10 Hz; if none arrive for
  `manual.max_age_s` (0.6 s) the aircraft is commanded to zero velocity (holds position).
  ArduPilot's own `GUID_TIMEOUT` (3 s) is a second layer.
* **Cloud-link failsafe** – if the relay link drops for `failsafe.link_loss_s` while
  airborne in a pilot-controlled mode, the agent commands `RTL` (or `LAND`). This is in
  addition to the FC's own radio/GCS failsafes, which you should still configure
  (`FS_GCS_ENABLE` etc.).
* **Altitude clamp** – `safety.max_alt` caps takeoff/goto altitude (default 120 m AGL).
* **Emergency stop** – hold the red button 2 s: force-disarm. Only for ground emergencies.
* **Page hidden → stick released** – switching apps on a phone releases the joystick.

## Protocol (for integrating your own client)

WebSocket text frames are JSON; binary frames are JPEG images.

Dashboard → drone: `{"type":"cmd","name":<name>,"args":{...}}` with names
`arm{confirm,force}` `disarm{force}` `takeoff{alt}` `land` `rtl` `mode{mode}` `goto{lat,lon,alt}`
`manual{pitch,roll,yaw,throttle ∈ [-1,1]}` `manual_stop` `set_home` `gimbal{pitch,yaw}`
`camera{source,fps,enabled}` `mission_upload{waypoints:[{lat,lon,alt}],takeoff_alt,rtl_at_end}`
`mission_start` `mission_clear` `kill{confirm:"KILL"}`.

Drone → dashboard: `telemetry` (5 Hz, see `mavlink_bridge.empty_state()` for fields),
`status{severity,text}` (FC STATUSTEXT), `ack{cmd,ok,msg}`, `dpong` (latency probe).
Server adds `drone_online` / `drone_offline` / `error`.

REST: `GET /api/health`, `GET /api/drones?token=…`.

## PX4 notes

The bridge is written against ArduPilot Copter. For PX4: use `OFFBOARD` instead of `GUIDED`
(and stream setpoints *before* switching mode), `AUTO.RTL` / `AUTO.LAND` / `AUTO.MISSION`
mode names, and `MAV_CMD_NAV_TAKEOFF` needs lat/lon filled. Those are the only places to change.

## Next steps worth doing

* Replace MJPEG-over-WebSocket with WebRTC (e.g. `aiortc` on the Pi + a TURN server) for
  lower latency and H.264 hardware encoding on the Pi 5.
* Per-user accounts / roles instead of a single shared token (FastAPI + JWT).
* Record telemetry + video server-side for post-flight review (fits the AiServe pipeline).
* Geofence editor on the map that uploads `FENCE_*` items to the FC.
