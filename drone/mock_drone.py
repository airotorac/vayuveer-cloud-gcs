"""
Simulated drone with the same interface as MavlinkBridge.

Lets you run the whole stack (agent -> relay -> dashboard) on a laptop with
no flight controller attached: `mode: mock` in config.yaml.
Simple kinematics, not a flight-dynamics model.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Optional

from mavlink_bridge import empty_state

EARTH_R = 6371000.0


def offset_latlon(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    dlat = north_m / EARTH_R
    dlon = east_m / (EARTH_R * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def dist_bearing(lat1, lon1, lat2, lon2) -> tuple[float, float]:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    brg = math.atan2(x, y)
    d = math.acos(max(-1, min(1, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))) * EARTH_R
    return d, brg


class MockDrone:
    def __init__(self, home_lat: float = 23.0225, home_lon: float = 72.5714,
                 on_status: Optional[Callable[[int, str], None]] = None,
                 on_ack: Optional[Callable[[str, bool, str], None]] = None, **_):
        self.on_status = on_status
        self.on_ack = on_ack
        self.state = empty_state()
        s = self.state
        s.update(connected=True, mode="STABILIZE", lat=home_lat, lon=home_lon, alt_msl=55.0,
                 battery_v=25.1, battery_pct=97, gps_fix="3D", sats=14, hdop=0.8, ekf_ok=True, rssi=88,
                 home={"lat": home_lat, "lon": home_lon, "alt": 55.0})
        self.home_alt = 55.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._target: dict | None = None          # {lat, lon, alt}
        self._vel = [0.0, 0.0, 0.0, 0.0]          # fwd, right, down, yaw_rate
        self._vel_t = 0.0
        self._mission: list[dict] = []
        self._mission_idx = 0
        self._armed_at: float | None = None
        self._landing = False

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="mock-drone").start()
        self._say(6, "Mock drone ready (no flight controller attached)")

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _say(self, sev: int, text: str) -> None:
        if self.on_status:
            self.on_status(sev, text)

    def _ack(self, name: str, ok: bool, msg: str = "") -> None:
        if self.on_ack:
            self.on_ack(name, ok, msg or ("ACCEPTED" if ok else "FAILED"))

    # ------------------------------------------------------------------ sim
    def _run(self) -> None:
        dt = 0.05
        last = time.time()
        while not self._stop.is_set():
            time.sleep(dt)
            now = time.time()
            step = now - last
            last = now
            with self._lock:
                self._tick(step, now)

    def _tick(self, dt: float, now: float) -> None:
        s = self.state
        s["roll"] *= 0.9
        s["pitch"] *= 0.9
        if s["armed"]:
            s["flight_time"] = now - (self._armed_at or now)
            s["battery_pct"] = max(0, s["battery_pct"] - dt * 0.05)
            s["battery_v"] = 21.0 + 4.2 * s["battery_pct"] / 100.0
            s["battery_a"] = 18.0 if s["alt_rel"] > 0.2 else 2.0
        else:
            s["battery_a"] = 0.4
            s["throttle"] = 0
            s["groundspeed"] = 0
            return

        mode = s["mode"]
        vn = ve = vd = 0.0
        yaw_rate = 0.0
        if mode == "GUIDED":
            if now - self._vel_t < 0.6 and any(abs(v) > 0.01 for v in self._vel):
                f, r, d, yr = self._vel
                h = math.radians(s["heading"])
                vn = f * math.cos(h) - r * math.sin(h)
                ve = f * math.sin(h) + r * math.cos(h)
                vd = d
                yaw_rate = yr
                s["pitch"] = -0.15 * f / 5.0
                s["roll"] = 0.15 * r / 5.0
                self._target = None
            elif self._target:
                vn, ve, vd, done = self._toward(self._target, 6.0)
                if done and self._target.get("takeoff"):
                    self._target = None
        elif mode == "AUTO":
            if self._mission_idx < len(self._mission):
                w = self._mission[self._mission_idx]
                vn, ve, vd, done = self._toward(w, 8.0)
                s["mission_current"] = self._mission_idx + 1
                if done:
                    self._mission_idx += 1
                    self._say(6, f"Reached waypoint {self._mission_idx}")
            else:
                self._say(6, "Mission complete, RTL")
                s["mode"] = "RTL"
        elif mode == "RTL":
            h = s["home"]
            vn, ve, vd, done = self._toward({"lat": h["lat"], "lon": h["lon"], "alt": max(s["alt_rel"], 15.0)}, 8.0)
            if done:
                s["mode"] = "LAND"
        elif mode == "LAND":
            vd = 1.5
        elif mode == "LOITER":
            pass

        if mode == "LAND" and s["alt_rel"] <= 0.05:
            s["alt_rel"] = 0.0
            s["armed"] = False
            self._armed_at = None
            s["mode"] = "GUIDED"
            self._say(6, "Landed, disarming")
            return

        s["alt_rel"] = max(0.0, s["alt_rel"] - vd * dt)
        s["alt_msl"] = self.home_alt + s["alt_rel"]
        s["lat"], s["lon"] = offset_latlon(s["lat"], s["lon"], vn * dt, ve * dt)
        s["vx"], s["vy"], s["vz"] = vn, ve, vd
        s["climb"] = -vd
        s["groundspeed"] = math.hypot(vn, ve)
        s["airspeed"] = s["groundspeed"]
        s["throttle"] = int(50 - vd * 10)
        if yaw_rate:
            s["heading"] = (s["heading"] + math.degrees(yaw_rate) * dt) % 360
        elif s["groundspeed"] > 0.5:
            s["heading"] = math.degrees(math.atan2(ve, vn)) % 360
        s["yaw"] = math.radians(s["heading"])

    def _toward(self, tgt: dict, speed: float) -> tuple[float, float, float, bool]:
        s = self.state
        d, brg = dist_bearing(s["lat"], s["lon"], tgt["lat"], tgt["lon"])
        dz = tgt["alt"] - s["alt_rel"]
        # climb first if we are low (mimics takeoff / RTL climb)
        if abs(dz) > 0.5 and d < 2.0:
            return 0.0, 0.0, -max(-2.5, min(2.5, dz)), False
        if d < 1.0 and abs(dz) <= 0.5:
            return 0.0, 0.0, 0.0, True
        v = min(speed, d)  # slow down near target
        vn, ve = v * math.cos(brg), v * math.sin(brg)
        vd = -max(-2.0, min(2.0, dz))
        return vn, ve, vd, False

    # ------------------------------------------------------------------ cmds
    def set_mode(self, mode: str) -> bool:
        mode = mode.upper()
        with self._lock:
            self.state["mode"] = mode
        self._ack("DO_SET_MODE", True)
        return True

    def arm(self, force: bool = False) -> bool:
        with self._lock:
            if self.state["armed"]:
                self._ack("COMPONENT_ARM_DISARM", True, "already armed")
                return True
            self.state["armed"] = True
            self._armed_at = time.time()
        self._say(6, "Arming motors")
        self._ack("COMPONENT_ARM_DISARM", True)
        return True

    def disarm(self, force: bool = False) -> bool:
        with self._lock:
            if self.state["alt_rel"] > 0.3 and not force:
                self._ack("COMPONENT_ARM_DISARM", False, "in flight (use force)")
                return False
            self.state["armed"] = False
            self.state["alt_rel"] = 0.0
            self._armed_at = None
        self._say(6, "Disarming motors")
        self._ack("COMPONENT_ARM_DISARM", True)
        return True

    def takeoff(self, alt: float) -> bool:
        with self._lock:
            if not self.state["armed"]:
                self._ack("NAV_TAKEOFF", False, "not armed")
                return False
            self.state["mode"] = "GUIDED"
            self._target = {"lat": self.state["lat"], "lon": self.state["lon"], "alt": float(alt), "takeoff": True}
        self._say(6, f"Taking off to {alt:.0f} m")
        self._ack("NAV_TAKEOFF", True)
        return True

    def land(self) -> bool:
        return self.set_mode("LAND")

    def rtl(self) -> bool:
        return self.set_mode("RTL")

    def goto(self, lat: float, lon: float, alt_rel: float) -> bool:
        with self._lock:
            self.state["mode"] = "GUIDED"
            self._target = {"lat": lat, "lon": lon, "alt": alt_rel}
            self._vel = [0, 0, 0, 0]
        self._say(6, f"Guided target set ({alt_rel:.0f} m)")
        return True

    def velocity_body(self, forward: float, right: float, down: float, yaw_rate: float) -> bool:
        with self._lock:
            self._vel = [forward, right, down, yaw_rate]
            self._vel_t = time.time()
        return True

    def set_home_here(self) -> bool:
        with self._lock:
            s = self.state
            s["home"] = {"lat": s["lat"], "lon": s["lon"], "alt": s["alt_msl"]}
        self._ack("DO_SET_HOME", True)
        return True

    def gimbal(self, pitch_deg: float, yaw_deg: float) -> bool:
        self._ack("DO_MOUNT_CONTROL", True)
        return True

    def mission_start(self) -> bool:
        with self._lock:
            if not self._mission:
                self._ack("MISSION_START", False, "no mission")
                return False
            if not self.state["armed"]:
                self._ack("MISSION_START", False, "not armed")
                return False
            self._mission_idx = 0
            self.state["mode"] = "AUTO"
        self._ack("MISSION_START", True)
        return True

    def mission_clear(self) -> bool:
        with self._lock:
            self._mission = []
            self.state["mission_count"] = 0
        return True

    def upload_mission(self, waypoints: list[dict], takeoff_alt: float = 10.0, rtl_at_end: bool = True) -> tuple[bool, str]:
        time.sleep(0.4)
        with self._lock:
            self._mission = [{"lat": w["lat"], "lon": w["lon"], "alt": float(w.get("alt", takeoff_alt))} for w in waypoints]
            self.state["mission_count"] = len(self._mission) + 2 + (1 if rtl_at_end else 0)
        return True, "ACCEPTED"
