"""Telemetry state shape shared by the MAVLink bridge and the mock drone."""


def empty_state() -> dict:
    return {
        "connected": False, "armed": False, "mode": "UNKNOWN", "system_status": 0,
        "lat": 0.0, "lon": 0.0, "alt_msl": 0.0, "alt_rel": 0.0,
        "vx": 0.0, "vy": 0.0, "vz": 0.0, "heading": 0.0,
        "roll": 0.0, "pitch": 0.0, "yaw": 0.0,
        "groundspeed": 0.0, "airspeed": 0.0, "climb": 0.0, "throttle": 0,
        "battery_v": 0.0, "battery_a": 0.0, "battery_pct": -1,
        "gps_fix": "NO GPS", "sats": 0, "hdop": 99.0,
        "home": None, "rssi": None, "ekf_ok": None,
        "mission_current": 0, "mission_count": 0,
        "flight_time": 0.0,
    }
