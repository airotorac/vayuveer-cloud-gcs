"""
AX4 companion agent - runs on the Raspberry Pi 5 on the aircraft.

  flight controller <--MAVLink--> this agent <--WebSocket (outbound)--> relay server <--> dashboards

Responsibilities
  * keep an outbound WebSocket to the relay (reconnects forever, works behind 4G NAT)
  * stream telemetry (JSON, 5 Hz) and camera frames (binary JPEG, configurable fps)
  * execute dashboard commands (arm, takeoff, goto, joystick velocity, missions...)
  * safety: joystick watchdog, link-loss failsafe (RTL/LAND), arm-confirmation flag

Usage:  python agent.py [config.yaml]
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import websockets
import yaml

from camera import make_camera

log = logging.getLogger("ax4.agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def load_config(path: str | None) -> dict:
    p = Path(path or os.environ.get("AX4_CONFIG", Path(__file__).with_name("config.yaml")))
    with open(p) as f:
        cfg = yaml.safe_load(f) or {}
    # env overrides for the secrets
    cfg["server_url"] = os.environ.get("AX4_SERVER_URL", cfg.get("server_url", "ws://127.0.0.1:8000"))
    cfg["token"] = os.environ.get("AX4_TOKEN", cfg.get("token", "change-me"))
    cfg["drone_id"] = os.environ.get("AX4_DRONE_ID", cfg.get("drone_id", "ax4-01"))
    return cfg


def rpi_stats() -> dict:
    out = {}
    try:
        out["cpu_temp"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0, 1)
    except Exception:
        pass
    try:
        out["load"] = round(os.getloadavg()[0], 2)
    except Exception:
        pass
    return out


class Agent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.loop = asyncio.get_event_loop()
        self.outbox: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
        self.ws = None
        self.link_lost_since: float | None = None
        self.failsafe_triggered = False

        # joystick state
        self.manual = {"f": 0.0, "r": 0.0, "d": 0.0, "yr": 0.0}
        self.manual_t = 0.0
        self.manual_active = False

        drone_cfg = cfg.get("drone", {})
        if drone_cfg.get("mode", "mock") == "mock":
            from mock_drone import MockDrone
            self.drone = MockDrone(home_lat=drone_cfg.get("mock_home_lat", 23.0225),
                                   home_lon=drone_cfg.get("mock_home_lon", 72.5714),
                                   on_status=self._on_status, on_ack=self._on_ack)
        else:
            from mavlink_bridge import MavlinkBridge
            self.drone = MavlinkBridge(drone_cfg.get("connection", "/dev/serial0"),
                                       baud=int(drone_cfg.get("baud", 921600)),
                                       on_status=self._on_status, on_ack=self._on_ack)
        self.camera = make_camera(cfg.get("camera", {}))
        self.video_enabled = bool(cfg.get("camera", {}).get("enabled", True))
        self.video_fps = float(cfg.get("camera", {}).get("fps", 10))

    # ---------------------------------------------------------------- callbacks (from drone thread)
    def _post(self, msg: dict) -> None:
        def _put():
            if self.outbox.full():
                try:
                    self.outbox.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self.outbox.put_nowait(msg)
        self.loop.call_soon_threadsafe(_put)

    def _on_status(self, severity: int, text: str) -> None:
        self._post({"type": "status", "severity": severity, "text": text, "t": time.time()})

    def _on_ack(self, cmd: str, ok: bool, msg: str) -> None:
        self._post({"type": "ack", "cmd": cmd, "ok": ok, "msg": msg, "t": time.time()})

    # ---------------------------------------------------------------- main
    async def run(self) -> None:
        self.drone.start()
        asyncio.create_task(self.manual_loop())
        asyncio.create_task(self.failsafe_loop())
        url = f"{self.cfg['server_url'].rstrip('/')}/ws/drone/{self.cfg['drone_id']}?token={self.cfg['token']}"
        backoff = 1
        while True:
            try:
                log.info("connecting to relay %s", url.split("?")[0])
                async with websockets.connect(url, ping_interval=10, ping_timeout=10, max_size=None,
                                              compression=None) as ws:
                    self.ws = ws
                    self.link_lost_since = None
                    self.failsafe_triggered = False
                    backoff = 1
                    log.info("relay connected")
                    self._on_status(6, f"Cloud link up ({self.cfg['drone_id']})")
                    await asyncio.gather(self.rx_loop(ws), self.telemetry_loop(ws),
                                         self.video_loop(ws), self.outbox_loop(ws))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("relay link down: %s", e)
            self.ws = None
            if self.link_lost_since is None:
                self.link_lost_since = time.time()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    async def rx_loop(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")
            if t == "cmd":
                await self.handle_cmd(msg)
            elif t == "dping":
                await ws.send(json.dumps({"type": "dpong", "t": msg.get("t"), "nonce": msg.get("nonce"),
                                          "drone_time": time.time()}))

    async def telemetry_loop(self, ws) -> None:
        period = 1.0 / float(self.cfg.get("telemetry_hz", 5))
        stats_t = 0.0
        stats = {}
        while True:
            s = self.drone.snapshot()
            now = time.time()
            if now - stats_t > 5:
                stats = rpi_stats()
                stats_t = now
            s.update(type="telemetry", t=now, id=self.cfg["drone_id"], rpi=stats,
                     video={"enabled": self.video_enabled, "fps": self.video_fps,
                            "source": getattr(self.camera, "source_label", "EO")},
                     manual_active=self.manual_active)
            self.camera.telemetry = s
            await ws.send(json.dumps(s))
            await asyncio.sleep(period)

    async def video_loop(self, ws) -> None:
        while True:
            if not self.video_enabled:
                await asyncio.sleep(0.5)
                continue
            t0 = time.time()
            try:
                frame = await asyncio.to_thread(self.camera.frame)
                await ws.send(frame)
            except websockets.ConnectionClosed:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("video frame error: %s", e)
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(max(0.0, 1.0 / self.video_fps - (time.time() - t0)))

    async def outbox_loop(self, ws) -> None:
        while True:
            msg = await self.outbox.get()
            await ws.send(json.dumps(msg))

    # ---------------------------------------------------------------- safety loops
    async def manual_loop(self) -> None:
        """Re-send joystick velocity at 10 Hz while fresh; zero it when the stick
        goes stale (dashboard closed, link hiccup)."""
        max_age = float(self.cfg.get("manual", {}).get("max_age_s", 0.6))
        while True:
            await asyncio.sleep(0.1)
            if not self.manual_active:
                continue
            if time.time() - self.manual_t > max_age:
                self.drone.velocity_body(0, 0, 0, 0)
                self.manual_active = False
                self._on_status(5, "Joystick input stale - holding position")
                continue
            m = self.manual
            self.drone.velocity_body(m["f"], m["r"], m["d"], m["yr"])

    async def failsafe_loop(self) -> None:
        fs = self.cfg.get("failsafe", {})
        action = fs.get("action", "rtl")
        after = float(fs.get("link_loss_s", 10))
        while True:
            await asyncio.sleep(1)
            if action == "none" or self.failsafe_triggered or self.link_lost_since is None:
                continue
            s = self.drone.snapshot()
            lost_for = time.time() - self.link_lost_since
            if lost_for >= after and s["armed"] and s["alt_rel"] > 1.0 and s["mode"] in ("GUIDED", "LOITER", "POSHOLD", "ALT_HOLD"):
                log.warning("cloud link lost %.0fs while airborne -> %s", lost_for, action.upper())
                self.failsafe_triggered = True
                if action == "land":
                    self.drone.land()
                else:
                    self.drone.rtl()
                self._on_status(2, f"Cloud link lost {lost_for:.0f}s: failsafe {action.upper()}")

    # ---------------------------------------------------------------- commands
    async def handle_cmd(self, msg: dict) -> None:
        name = msg.get("name")
        a = msg.get("args") or {}
        d = self.drone
        log.info("cmd %s %s", name, a if name != "manual" else "")
        try:
            if name == "manual":
                lim = self.cfg.get("manual", {})
                vmax = float(lim.get("max_speed_mps", 5.0))
                vzmax = float(lim.get("max_climb_mps", 2.0))
                yrmax = float(lim.get("max_yaw_rate_dps", 60.0))
                self.manual = {
                    "f": _clamp(a.get("pitch", 0)) * vmax,
                    "r": _clamp(a.get("roll", 0)) * vmax,
                    "d": -_clamp(a.get("throttle", 0)) * vzmax,   # stick up = climb = negative down
                    "yr": _clamp(a.get("yaw", 0)) * yrmax * 3.14159 / 180.0,
                }
                self.manual_t = time.time()
                if not self.manual_active:
                    s = d.snapshot()
                    if s["mode"] != "GUIDED":
                        d.set_mode("GUIDED")
                    self.manual_active = True
                return
            if name == "manual_stop":
                self.manual_active = False
                d.velocity_body(0, 0, 0, 0)
                return
            if name == "arm":
                if self.cfg.get("safety", {}).get("require_arm_confirm", True) and not a.get("confirm"):
                    self._on_ack("ARM", False, "confirmation flag missing")
                    return
                d.arm(force=bool(a.get("force")))
            elif name == "disarm":
                d.disarm(force=bool(a.get("force")))
            elif name == "takeoff":
                alt = float(a.get("alt", self.cfg.get("safety", {}).get("default_takeoff_alt", 10)))
                alt = min(alt, float(self.cfg.get("safety", {}).get("max_alt", 120)))
                d.takeoff(alt)
            elif name == "land":
                self.manual_active = False
                d.land()
            elif name == "rtl":
                self.manual_active = False
                d.rtl()
            elif name == "mode":
                self.manual_active = False
                d.set_mode(str(a.get("mode", "LOITER")))
            elif name == "goto":
                self.manual_active = False
                alt = min(float(a.get("alt", 20)), float(self.cfg.get("safety", {}).get("max_alt", 120)))
                d.goto(float(a["lat"]), float(a["lon"]), alt)
            elif name == "set_home":
                d.set_home_here()
            elif name == "gimbal":
                d.gimbal(float(a.get("pitch", 0)), float(a.get("yaw", 0)))
            elif name == "camera":
                if "source" in a:
                    self.camera.source_label = str(a["source"]).upper()
                if "enabled" in a:
                    self.video_enabled = bool(a["enabled"])
                if "fps" in a:
                    self.video_fps = max(1.0, min(30.0, float(a["fps"])))
                self._on_ack("CAMERA", True, f"{self.camera.source_label} {self.video_fps:.0f}fps")
            elif name == "mission_upload":
                wps = a.get("waypoints", [])
                ok, res = await asyncio.to_thread(d.upload_mission, wps,
                                                  float(a.get("takeoff_alt", 10)), bool(a.get("rtl_at_end", True)))
                self._on_ack("MISSION_UPLOAD", ok, f"{res} ({len(wps)} wps)")
            elif name == "mission_start":
                self.manual_active = False
                d.mission_start()
            elif name == "mission_clear":
                d.mission_clear()
            elif name == "kill":
                # Emergency motor stop. Only honoured with explicit confirm.
                if a.get("confirm") == "KILL":
                    self.manual_active = False
                    d.disarm(force=True)
                    self._on_status(0, "EMERGENCY STOP - motors killed")
            else:
                self._on_ack(str(name), False, "unknown command")
        except Exception as e:  # noqa: BLE001
            log.exception("command %s failed", name)
            self._on_ack(str(name).upper(), False, str(e))


def _clamp(v, lo=-1.0, hi=1.0) -> float:
    try:
        v = float(v)
    except Exception:
        return 0.0
    return max(lo, min(hi, v))


def main() -> None:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    agent = Agent(cfg)
    task = loop.create_task(agent.run())

    def _stop(*_):
        task.cancel()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        agent.drone.stop()
        agent.camera.close()


if __name__ == "__main__":
    main()
