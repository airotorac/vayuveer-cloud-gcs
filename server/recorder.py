"""
Flight recorder for the relay.

One recording = one flight (started automatically when the aircraft arms, stopped when it
disarms) or a manual recording started by an operator.  Each flight is a directory:

  <log_dir>/<drone_id>/<YYYYmmdd-HHMMSS>/
      telemetry.jsonl   every telemetry message (5 Hz)
      events.jsonl      FC status text, command acks, and every dashboard command with the user
      video.mjpeg       raw JPEG stream while recording (deleted after transcoding)
      video.mp4         H.264 video produced by ffmpeg when the flight ends
      summary.json      stats: duration, max altitude, distance, battery, breaches, users...

Retention: oldest flights are deleted when the log directory exceeds VAYUVEER_MAX_LOG_GB.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("vayuveer.recorder")

FFMPEG = shutil.which("ffmpeg")


def _haversine(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371000.0 * 2 * math.asin(math.sqrt(a))


class FlightRecorder:
    def __init__(self, drone_id: str, base_dir: Path, max_gb: float = 5.0):
        self.drone_id = drone_id
        self.base_dir = Path(base_dir)
        self.max_gb = max_gb
        self.active = False
        self.manual = False
        self.flight_id: str | None = None
        self.started = 0.0
        self.started_by: str | None = None
        self.dir: Path | None = None
        self._tele = self._events = self._video = None
        self._lock = threading.Lock()
        self.frames = 0
        self._stats: dict = {}
        self._last_pos: tuple[float, float] | None = None
        self._pending: deque[tuple[float, str, dict]] = deque(maxlen=8)   # commands seen just before a flight started (e.g. ARM)

    # ------------------------------------------------------------------ control
    def status(self) -> dict:
        return {"active": self.active, "manual": self.manual, "flight_id": self.flight_id,
                "since": self.started if self.active else None, "frames": self.frames}

    def start(self, reason: str, user: str | None = None) -> str:
        with self._lock:
            if self.active:
                return self.flight_id  # type: ignore[return-value]
            self.flight_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            self.dir = self.base_dir / self.drone_id / self.flight_id
            self.dir.mkdir(parents=True, exist_ok=True)
            self._tele = open(self.dir / "telemetry.jsonl", "a", buffering=1 << 16)
            self._events = open(self.dir / "events.jsonl", "a", buffering=1 << 12)
            self._video = open(self.dir / "video.mjpeg", "ab", buffering=1 << 20)
            self.started = time.time()
            self.started_by = user
            self.manual = reason == "manual"
            self.frames = 0
            self._last_pos = None
            self._stats = {"max_alt": 0.0, "max_dist_home": 0.0, "distance_m": 0.0, "min_sats": None,
                           "batt_start": None, "batt_end": None, "modes": [], "breaches": 0, "users": [],
                           "commands": 0, "samples": 0, "max_groundspeed": 0.0, "home": None, "_breach": False}
            self.active = True
            log.info("[%s] recording started (%s) -> %s", self.drone_id, reason, self.dir)
            self._write_summary(status="recording", reason=reason)
            self._events.write(json.dumps({"type": "recording", "t": self.started, "text": f"recording started ({reason})", "user": user}) + "\n")
            for t, u, payload in list(self._pending):
                if self.started - t < 15:
                    self._log_command(u, payload, t)
            self._pending.clear()
            return self.flight_id

    def stop(self) -> dict | None:
        with self._lock:
            if not self.active:
                return None
            self.active = False
            ended = time.time()
            for f in (self._tele, self._events, self._video):
                try:
                    f.close()
                except Exception:
                    pass
            self._tele = self._events = self._video = None
            summary = self._write_summary(status="processing" if self.frames and FFMPEG else "ready", ended=ended)
            d, fid, frames, duration = self.dir, self.flight_id, self.frames, max(1.0, ended - self.started)
            log.info("[%s] recording stopped: %s (%.0fs, %d frames)", self.drone_id, fid, duration, frames)
        threading.Thread(target=self._finalize, args=(d, frames, duration), daemon=True).start()
        return summary

    # ------------------------------------------------------------------ data in
    def on_telemetry(self, tele: dict, raw: str) -> None:
        if not self.active or self._tele is None:
            return
        try:
            self._tele.write(raw + "\n")
        except Exception:
            return
        st = self._stats
        st["samples"] += 1
        lat, lon = tele.get("lat") or 0.0, tele.get("lon") or 0.0
        alt = float(tele.get("alt_rel") or 0.0)
        st["max_alt"] = max(st["max_alt"], alt)
        st["max_groundspeed"] = max(st["max_groundspeed"], float(tele.get("groundspeed") or 0.0))
        sats = tele.get("sats")
        if sats is not None:
            st["min_sats"] = sats if st["min_sats"] is None else min(st["min_sats"], sats)
        bp = tele.get("battery_pct")
        if bp is not None and bp >= 0:
            if st["batt_start"] is None:
                st["batt_start"] = bp
            st["batt_end"] = bp
        mode = tele.get("mode")
        if mode and mode not in st["modes"]:
            st["modes"].append(mode)
        home = tele.get("home")
        if home and not st["home"]:
            st["home"] = {"lat": home.get("lat"), "lon": home.get("lon")}
        if lat and lon:
            if self._last_pos:
                d = _haversine(self._last_pos[0], self._last_pos[1], lat, lon)
                if 0.05 < d < 500:   # ignore GPS jitter and glitches
                    st["distance_m"] += d
            self._last_pos = (lat, lon)
            if st["home"] and st["home"]["lat"]:
                st["max_dist_home"] = max(st["max_dist_home"], _haversine(st["home"]["lat"], st["home"]["lon"], lat, lon))
        breach = bool(tele.get("fence_breach"))
        if breach and not st["_breach"]:
            st["breaches"] += 1
        st["_breach"] = breach

    def on_event(self, raw: str) -> None:
        if self.active and self._events is not None:
            try:
                self._events.write(raw + "\n")
            except Exception:
                pass

    def on_command(self, user: str, payload: dict) -> None:
        if payload.get("name") == "manual":
            return  # 10 Hz joystick packets would swamp the log; the effect is visible in telemetry
        if not self.active or self._events is None:
            self._pending.append((time.time(), user, payload))
            return
        self._log_command(user, payload, time.time())

    def _log_command(self, user: str, payload: dict, t: float) -> None:
        st = self._stats
        st["commands"] += 1
        if user not in st["users"]:
            st["users"].append(user)
        try:
            self._events.write(json.dumps({"type": "command", "t": t, "user": user,
                                           "name": payload.get("name"), "args": payload.get("args")}) + "\n")
        except Exception:
            pass

    def on_frame(self, data: bytes) -> None:
        if self.active and self._video is not None:
            try:
                self._video.write(data)
                self.frames += 1
            except Exception:
                pass

    # ------------------------------------------------------------------ files
    def _write_summary(self, **extra) -> dict:
        st = {k: v for k, v in self._stats.items() if not k.startswith("_")}
        ended = extra.get("ended")
        summary = {
            "drone_id": self.drone_id, "flight_id": self.flight_id, "started": self.started,
            "started_by": self.started_by, "manual": self.manual,
            "ended": ended, "duration_s": round(ended - self.started, 1) if ended else None,
            "frames": self.frames, "video": "processing" if extra.get("status") == "processing" else ("none" if not self.frames else "raw"),
            "status": extra.get("status", "recording"), "reason": extra.get("reason"),
            **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in st.items()},
        }
        if self.dir:
            try:
                (self.dir / "summary.json").write_text(json.dumps(summary, indent=1))
            except Exception:
                pass
        return summary

    def _finalize(self, d: Path, frames: int, duration: float) -> None:
        """Transcode the JPEG stream to MP4, then enforce retention."""
        raw = d / "video.mjpeg"
        result = "none"
        if frames and raw.exists() and FFMPEG:
            fps = max(1.0, min(30.0, frames / duration))
            cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "mjpeg", "-framerate", f"{fps:.2f}", "-i", str(raw),
                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(d / "video.mp4")]
            try:
                subprocess.run(cmd, check=True, timeout=1800)
                raw.unlink(missing_ok=True)
                result = "ready"
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] ffmpeg failed for %s: %s", self.drone_id, d.name, e)
                result = "raw"
        elif frames and raw.exists():
            result = "raw"
        elif raw.exists():
            raw.unlink(missing_ok=True)
        try:
            p = d / "summary.json"
            s = json.loads(p.read_text())
            s["video"], s["status"] = result, "ready"
            s["size_mb"] = round(sum(f.stat().st_size for f in d.iterdir()) / 1e6, 2)
            p.write_text(json.dumps(s, indent=1))
        except Exception:
            pass
        enforce_retention(self.base_dir, self.max_gb)


# ---------------------------------------------------------------------- queries
def _safe_id(s: str) -> bool:
    return bool(s) and all(c.isalnum() or c in "-_." for c in s) and ".." not in s


def flight_dir(base_dir: Path, drone_id: str, flight_id: str) -> Path | None:
    if not (_safe_id(drone_id) and _safe_id(flight_id)):
        return None
    d = Path(base_dir) / drone_id / flight_id
    return d if d.is_dir() else None


def list_flights(base_dir: Path, drone_id: str | None = None) -> list[dict]:
    out = []
    base = Path(base_dir)
    if not base.exists():
        return out
    for dd in sorted(base.iterdir()):
        if not dd.is_dir() or (drone_id and dd.name != drone_id):
            continue
        for fd in sorted(dd.iterdir(), reverse=True):
            p = fd / "summary.json"
            if p.exists():
                try:
                    out.append(json.loads(p.read_text()))
                except Exception:
                    pass
    out.sort(key=lambda s: s.get("started", 0), reverse=True)
    return out


def track(d: Path, max_points: int = 2000) -> list:
    """Decimated [t, lat, lon, alt_rel, mode] list for drawing the flown path."""
    p = d / "telemetry.jsonl"
    if not p.exists():
        return []
    pts = []
    with open(p) as f:
        for line in f:
            try:
                t = json.loads(line)
            except Exception:
                continue
            if t.get("lat") and t.get("lon"):
                pts.append([round(t.get("t", 0), 2), t["lat"], t["lon"], round(t.get("alt_rel", 0), 1), t.get("mode")])
    if len(pts) > max_points:
        step = math.ceil(len(pts) / max_points)
        pts = pts[::step]
    return pts


def delete_flight(d: Path) -> None:
    shutil.rmtree(d, ignore_errors=True)


def enforce_retention(base_dir: Path, max_gb: float) -> None:
    base = Path(base_dir)
    if not base.exists():
        return
    flights = []
    for dd in base.iterdir():
        if dd.is_dir():
            for fd in dd.iterdir():
                if fd.is_dir():
                    size = sum(f.stat().st_size for f in fd.iterdir() if f.is_file())
                    flights.append((fd.name, fd, size))
    total = sum(s for _, _, s in flights)
    limit = max_gb * 1e9
    for _, fd, size in sorted(flights):          # oldest first (ids are timestamps)
        if total <= limit:
            break
        if (fd / "summary.json").exists() and json.loads((fd / "summary.json").read_text()).get("status") == "recording":
            continue
        log.info("retention: deleting %s (%.1f MB)", fd, size / 1e6)
        delete_flight(fd)
        total -= size
