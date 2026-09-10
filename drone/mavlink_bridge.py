"""
MAVLink bridge for the AX4 companion computer (Raspberry Pi 5).

Talks to the flight controller (ArduPilot Copter; PX4 mostly compatible, see
README) over serial or UDP using pymavlink, keeps a live state dict, and
exposes high-level commands the agent maps 1:1 to dashboard buttons.

Thread model: one reader thread pumps MAVLink messages into `self.state`.
Command methods are called from the asyncio loop and are non-blocking except
`upload_mission`, which the agent runs in a worker thread.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as mavlink

log = logging.getLogger("vayuveer.mavlink")

# Fallback if the FC has not told us its mode map yet.
COPTER_MODES = {
    "STABILIZE": 0, "ACRO": 1, "ALT_HOLD": 2, "AUTO": 3, "GUIDED": 4, "LOITER": 5,
    "RTL": 6, "CIRCLE": 7, "LAND": 9, "DRIFT": 11, "SPORT": 13, "FLIP": 14,
    "AUTOTUNE": 15, "POSHOLD": 16, "BRAKE": 17, "THROW": 18, "AVOID_ADSB": 19,
    "GUIDED_NOGPS": 20, "SMART_RTL": 21, "FLOWHOLD": 22, "FOLLOW": 23,
    "ZIGZAG": 24, "SYSTEMID": 25, "AUTOROTATE": 26, "AUTO_RTL": 27,
}

GPS_FIX = {0: "NO GPS", 1: "NO FIX", 2: "2D", 3: "3D", 4: "DGPS", 5: "RTK FLOAT", 6: "RTK FIX"}

# type_mask bits for SET_POSITION_TARGET_*
_IGNORE_PX, _IGNORE_PY, _IGNORE_PZ = 1, 2, 4
_IGNORE_VX, _IGNORE_VY, _IGNORE_VZ = 8, 16, 32
_IGNORE_AX, _IGNORE_AY, _IGNORE_AZ = 64, 128, 256
_FORCE, _IGNORE_YAW, _IGNORE_YAW_RATE = 512, 1024, 2048
MASK_POS_ONLY = _IGNORE_VX | _IGNORE_VY | _IGNORE_VZ | _IGNORE_AX | _IGNORE_AY | _IGNORE_AZ | _IGNORE_YAW | _IGNORE_YAW_RATE
MASK_VEL_YAWRATE = _IGNORE_PX | _IGNORE_PY | _IGNORE_PZ | _IGNORE_AX | _IGNORE_AY | _IGNORE_AZ | _IGNORE_YAW


from state import empty_state  # noqa: E402,F401  (re-exported for callers)


class MavlinkBridge:
    def __init__(self, connection: str, baud: int = 921600, source_system: int = 255,
                 on_status: Optional[Callable[[int, str], None]] = None,
                 on_ack: Optional[Callable[[str, bool, str], None]] = None):
        self.connection = connection
        self.baud = baud
        self.source_system = source_system
        self.on_status = on_status
        self.on_ack = on_ack
        self.state = empty_state()
        self.master: mavutil.mavfile | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mission_q: "queue.Queue[object]" = queue.Queue()
        self._mission_busy = False
        self._armed_since: float | None = None
        self._mode_map: dict[str, int] = dict(COPTER_MODES)

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mavlink-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _connect(self) -> None:
        log.info("connecting to flight controller at %s", self.connection)
        self.master = mavutil.mavlink_connection(self.connection, baud=self.baud,
                                                 source_system=self.source_system, source_component=191)
        self.master.wait_heartbeat(timeout=30)
        log.info("heartbeat from system %d component %d", self.master.target_system, self.master.target_component)
        mm = self.master.mode_mapping()
        if mm:
            self._mode_map = dict(mm)
        self._request_streams()
        with self._lock:
            self.state["connected"] = True

    def _request_streams(self) -> None:
        m = self.master
        m.mav.request_data_stream_send(m.target_system, m.target_component,
                                       mavlink.MAV_DATA_STREAM_ALL, 4, 1)
        # Bump the ones the dashboard cares about.
        for msg_id, hz in ((mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, 5),
                           (mavlink.MAVLINK_MSG_ID_ATTITUDE, 10),
                           (mavlink.MAVLINK_MSG_ID_VFR_HUD, 5),
                           (mavlink.MAVLINK_MSG_ID_SYS_STATUS, 2),
                           (mavlink.MAVLINK_MSG_ID_GPS_RAW_INT, 2)):
            m.mav.command_long_send(m.target_system, m.target_component,
                                    mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                                    msg_id, int(1e6 / hz), 0, 0, 0, 0, 0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect()
                self._read_loop()
            except Exception as e:  # noqa: BLE001
                log.error("mavlink link error: %s", e)
                with self._lock:
                    self.state["connected"] = False
                time.sleep(2)

    # ------------------------------------------------------------------ read
    def _read_loop(self) -> None:
        last_hb = time.time()
        while not self._stop.is_set():
            msg = self.master.recv_match(blocking=True, timeout=1.0)
            if msg is None:
                if time.time() - last_hb > 5:
                    raise RuntimeError("heartbeat lost")
                continue
            t = msg.get_type()
            if t == "BAD_DATA":
                continue
            with self._lock:
                s = self.state
                if t == "HEARTBEAT":
                    if msg.type in (mavlink.MAV_TYPE_GCS,):
                        continue
                    last_hb = time.time()
                    armed = bool(msg.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                    if armed and not s["armed"]:
                        self._armed_since = time.time()
                    if not armed:
                        self._armed_since = None
                    s["armed"] = armed
                    s["system_status"] = msg.system_status
                    inv = {v: k for k, v in self._mode_map.items()}
                    s["mode"] = inv.get(msg.custom_mode, f"MODE{msg.custom_mode}")
                elif t == "GLOBAL_POSITION_INT":
                    s["lat"] = msg.lat / 1e7
                    s["lon"] = msg.lon / 1e7
                    s["alt_msl"] = msg.alt / 1000.0
                    s["alt_rel"] = msg.relative_alt / 1000.0
                    s["vx"], s["vy"], s["vz"] = msg.vx / 100.0, msg.vy / 100.0, msg.vz / 100.0
                    s["heading"] = msg.hdg / 100.0 if msg.hdg != 65535 else s["heading"]
                elif t == "ATTITUDE":
                    s["roll"], s["pitch"], s["yaw"] = msg.roll, msg.pitch, msg.yaw
                elif t == "VFR_HUD":
                    s["airspeed"] = msg.airspeed
                    s["groundspeed"] = msg.groundspeed
                    s["climb"] = msg.climb
                    s["throttle"] = msg.throttle
                elif t == "SYS_STATUS":
                    s["battery_v"] = msg.voltage_battery / 1000.0
                    s["battery_a"] = msg.current_battery / 100.0 if msg.current_battery != -1 else 0.0
                    s["battery_pct"] = msg.battery_remaining
                elif t == "GPS_RAW_INT":
                    s["gps_fix"] = GPS_FIX.get(msg.fix_type, str(msg.fix_type))
                    s["sats"] = msg.satellites_visible
                    s["hdop"] = msg.eph / 100.0 if msg.eph != 65535 else 99.0
                elif t == "HOME_POSITION":
                    s["home"] = {"lat": msg.latitude / 1e7, "lon": msg.longitude / 1e7, "alt": msg.altitude / 1000.0}
                elif t == "EKF_STATUS_REPORT":
                    # healthy if no variance flags are bad; ArduPilot sets bit 10 (uninitialised) when not ready.
                    s["ekf_ok"] = bool(msg.flags & mavlink.EKF_POS_HORIZ_ABS) and not bool(msg.flags & mavlink.EKF_UNINITIALIZED)
                elif t == "RC_CHANNELS":
                    s["rssi"] = msg.rssi if msg.rssi != 255 else None
                elif t == "RADIO_STATUS":
                    s["rssi"] = msg.rssi
                elif t == "MISSION_CURRENT":
                    s["mission_current"] = msg.seq
                elif t == "MISSION_COUNT" and not self._mission_busy:
                    s["mission_count"] = msg.count
                elif t == "STATUSTEXT":
                    text = msg.text.rstrip("\x00")
                    log.info("FC[%d]: %s", msg.severity, text)
                    if self.on_status:
                        self.on_status(msg.severity, text)
                elif t == "COMMAND_ACK":
                    name = mavlink.enums["MAV_CMD"].get(msg.command)
                    name = name.name if name else str(msg.command)
                    ok = msg.result == mavlink.MAV_RESULT_ACCEPTED
                    res = mavlink.enums["MAV_RESULT"].get(msg.result)
                    if self.on_ack:
                        self.on_ack(name, ok, res.name if res else str(msg.result))
                elif t in ("MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK") and self._mission_busy:
                    self._mission_q.put(msg)
                if self._armed_since:
                    s["flight_time"] = time.time() - self._armed_since

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    # ------------------------------------------------------------------ cmds
    def _ready(self) -> bool:
        return self.master is not None and self.state.get("connected", False)

    def set_mode(self, mode: str) -> bool:
        if not self._ready():
            return False
        mode = mode.upper()
        if mode not in self._mode_map:
            log.warning("unknown mode %s", mode)
            return False
        self.master.set_mode(self._mode_map[mode])
        return True

    def arm(self, force: bool = False) -> bool:
        if not self._ready():
            return False
        m = self.master
        m.mav.command_long_send(m.target_system, m.target_component,
                                mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0,
                                1, 21196 if force else 0, 0, 0, 0, 0, 0)
        return True

    def disarm(self, force: bool = False) -> bool:
        if not self._ready():
            return False
        m = self.master
        m.mav.command_long_send(m.target_system, m.target_component,
                                mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0,
                                0, 21196 if force else 0, 0, 0, 0, 0, 0)
        return True

    def takeoff(self, alt: float) -> bool:
        if not self._ready():
            return False
        m = self.master
        self.set_mode("GUIDED")
        m.mav.command_long_send(m.target_system, m.target_component,
                                mavlink.MAV_CMD_NAV_TAKEOFF, 0,
                                0, 0, 0, 0, 0, 0, float(alt))
        return True

    def land(self) -> bool:
        return self.set_mode("LAND")

    def rtl(self) -> bool:
        return self.set_mode("RTL")

    def goto(self, lat: float, lon: float, alt_rel: float) -> bool:
        """Fly to a GPS point at a relative altitude (GUIDED mode)."""
        if not self._ready():
            return False
        m = self.master
        if self.state["mode"] != "GUIDED":
            self.set_mode("GUIDED")
        m.mav.set_position_target_global_int_send(
            0, m.target_system, m.target_component,
            mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, MASK_POS_ONLY,
            int(lat * 1e7), int(lon * 1e7), float(alt_rel),
            0, 0, 0, 0, 0, 0, 0, 0)
        return True

    def velocity_body(self, forward: float, right: float, down: float, yaw_rate: float) -> bool:
        """Joystick control: m/s in body frame + yaw rate rad/s. Must be refreshed
        continuously; ArduPilot stops after GUID_TIMEOUT (3 s) without updates."""
        if not self._ready():
            return False
        m = self.master
        m.mav.set_position_target_local_ned_send(
            0, m.target_system, m.target_component,
            mavlink.MAV_FRAME_BODY_OFFSET_NED, MASK_VEL_YAWRATE,
            0, 0, 0, float(forward), float(right), float(down), 0, 0, 0, 0, float(yaw_rate))
        return True

    def set_home_here(self) -> bool:
        if not self._ready():
            return False
        m = self.master
        m.mav.command_long_send(m.target_system, m.target_component,
                                mavlink.MAV_CMD_DO_SET_HOME, 0, 1, 0, 0, 0, 0, 0, 0)
        return True

    def gimbal(self, pitch_deg: float, yaw_deg: float) -> bool:
        """Point the gimbal (MAV_CMD_DO_MOUNT_CONTROL, works with ArduPilot mounts)."""
        if not self._ready():
            return False
        m = self.master
        m.mav.command_long_send(m.target_system, m.target_component,
                                mavlink.MAV_CMD_DO_MOUNT_CONTROL, 0,
                                float(pitch_deg), 0, float(yaw_deg), 0, 0, 0,
                                mavlink.MAV_MOUNT_MODE_MAVLINK_TARGETING)
        return True

    def mission_start(self) -> bool:
        if not self._ready():
            return False
        m = self.master
        m.mav.command_long_send(m.target_system, m.target_component,
                                mavlink.MAV_CMD_MISSION_START, 0, 0, 0, 0, 0, 0, 0, 0)
        return self.set_mode("AUTO")

    def mission_clear(self) -> bool:
        if not self._ready():
            return False
        m = self.master
        m.mav.mission_clear_all_send(m.target_system, m.target_component)
        return True

    def upload_mission(self, waypoints: list[dict], takeoff_alt: float = 10.0, rtl_at_end: bool = True) -> tuple[bool, str]:
        """Blocking. waypoints: [{lat, lon, alt}] with alt relative to home.
        Builds: seq0 home placeholder, seq1 TAKEOFF, waypoints..., optional RTL."""
        if not self._ready():
            return False, "not connected"
        m = self.master
        s = self.snapshot()
        items = []

        def wp(seq, cmd, lat, lon, alt, p1=0, p2=0, p3=0, p4=0, frame=mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT):
            return mavlink.MAVLink_mission_item_int_message(
                m.target_system, m.target_component, seq, frame, cmd, 0, 1,
                p1, p2, p3, p4, int(lat * 1e7), int(lon * 1e7), float(alt),
                mavlink.MAV_MISSION_TYPE_MISSION)

        home = s["home"] or {"lat": s["lat"], "lon": s["lon"], "alt": 0}
        items.append(wp(0, mavlink.MAV_CMD_NAV_WAYPOINT, home["lat"], home["lon"], 0, frame=mavlink.MAV_FRAME_GLOBAL_INT))
        items.append(wp(1, mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, takeoff_alt))
        for i, w in enumerate(waypoints, start=2):
            items.append(wp(i, mavlink.MAV_CMD_NAV_WAYPOINT, w["lat"], w["lon"], w.get("alt", takeoff_alt),
                            p1=w.get("hold", 0)))
        if rtl_at_end:
            items.append(wp(len(items), mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH, 0, 0, 0))

        self._mission_busy = True
        while not self._mission_q.empty():
            self._mission_q.get_nowait()
        try:
            m.mav.mission_count_send(m.target_system, m.target_component, len(items), mavlink.MAV_MISSION_TYPE_MISSION)
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    msg = self._mission_q.get(timeout=3)
                except queue.Empty:
                    return False, "FC did not request mission items"
                t = msg.get_type()
                if t in ("MISSION_REQUEST", "MISSION_REQUEST_INT"):
                    if msg.seq >= len(items):
                        return False, f"bad seq {msg.seq}"
                    m.mav.send(items[msg.seq])
                elif t == "MISSION_ACK":
                    ok = msg.type == mavlink.MAV_MISSION_ACCEPTED
                    res = mavlink.enums["MAV_MISSION_RESULT"].get(msg.type)
                    with self._lock:
                        self.state["mission_count"] = len(items) if ok else self.state["mission_count"]
                    return ok, res.name if res else str(msg.type)
            return False, "mission upload timed out"
        finally:
            self._mission_busy = False
