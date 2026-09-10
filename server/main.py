"""
VayuVeer Cloud GCS - relay server.

Sits on the public internet. Drones (RPi 5 agents) connect OUT to it over a
WebSocket, dashboards connect to it too, and the server relays:

  dashboard  --(JSON commands)-->  server  --> drone
  drone      --(JSON telemetry, binary JPEG frames)--> server --> all dashboards

Neither side needs a public IP or port-forwarding, which is what makes it work
over 4G/5G and behind NAT.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
Env:  VAYUVEER_TOKEN   shared secret both sides must present (?token=...)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Dict, Set

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
import recorder as rec

log = logging.getLogger("vayuveer.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TOKEN = os.environ.get("VAYUVEER_TOKEN", "change-me")
DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
LOG_DIR = Path(os.environ.get("VAYUVEER_LOG_DIR", Path(__file__).resolve().parent.parent / "data" / "flights"))
MAX_LOG_GB = float(os.environ.get("VAYUVEER_MAX_LOG_GB", "5"))

app = FastAPI(title="VayuVeer Cloud GCS relay")


class DroneRoom:
    """Everything connected for one drone id."""

    def __init__(self, drone_id: str):
        self.drone_id = drone_id
        self.drone: WebSocket | None = None
        self.clients: Set[WebSocket] = set()
        self.last_telemetry: dict | None = None
        self.recent_events: deque[str] = deque(maxlen=50)   # status/ack replayed to new dashboards
        self.rec = rec.FlightRecorder(drone_id, LOG_DIR, MAX_LOG_GB)
        self.last_seen = 0.0
        self.frames = 0
        self.bytes_video = 0

    def status(self) -> dict:
        return {
            "id": self.drone_id,
            "online": self.drone is not None,
            "clients": len(self.clients),
            "last_seen": self.last_seen,
            "frames": self.frames,
            "video_mb": round(self.bytes_video / 1e6, 2),
        }


rooms: Dict[str, DroneRoom] = {}


def room(drone_id: str) -> DroneRoom:
    if drone_id not in rooms:
        rooms[drone_id] = DroneRoom(drone_id)
    return rooms[drone_id]


async def _send_json_safe(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_text(json.dumps(payload))
        return True
    except Exception:
        return False


async def broadcast_text(r: DroneRoom, text: str) -> None:
    dead = []
    for c in list(r.clients):
        try:
            await c.send_text(text)
        except Exception:
            dead.append(c)
    for c in dead:
        r.clients.discard(c)


async def broadcast_bytes(r: DroneRoom, data: bytes) -> None:
    dead = []
    for c in list(r.clients):
        try:
            await c.send_bytes(data)
        except Exception:
            dead.append(c)
    for c in dead:
        r.clients.discard(c)


async def broadcast_recording(r: DroneRoom) -> None:
    await broadcast_text(r, json.dumps({"type": "recording", **r.rec.status()}))


def _auth_ok(token: str | None) -> bool:
    """Drone side: master token only."""
    return token is not None and token == TOKEN


def _principal(token: str | None) -> dict | None:
    """Dashboard side: master token or a user session."""
    if not token:
        return None
    return auth.authenticate(token, TOKEN)


# --------------------------------------------------------------------------- #
# Drone side
# --------------------------------------------------------------------------- #
@app.websocket("/ws/drone/{drone_id}")
async def ws_drone(ws: WebSocket, drone_id: str, token: str | None = Query(default=None)):
    if not _auth_ok(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    r = room(drone_id)
    if r.drone is not None:
        # A previous socket is still registered (e.g. drone rebooted before the
        # old one timed out). Replace it.
        try:
            await r.drone.close(code=4409)
        except Exception:
            pass
    r.drone = ws
    r.last_seen = time.time()
    log.info("drone %s connected", drone_id)
    await broadcast_text(r, json.dumps({"type": "drone_online", "id": drone_id}))

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            r.last_seen = time.time()
            if msg.get("bytes") is not None:
                data = msg["bytes"]
                r.frames += 1
                r.bytes_video += len(data)
                r.rec.on_frame(data)
                await broadcast_bytes(r, data)
            elif msg.get("text") is not None:
                text = msg["text"]
                # Cache last telemetry so a freshly opened dashboard gets state immediately.
                try:
                    msg_obj = json.loads(text) if text.startswith("{") else None
                except Exception:
                    msg_obj = None
                mtype = msg_obj.get("type") if isinstance(msg_obj, dict) else None
                if mtype == "telemetry":
                    tele = msg_obj
                    r.last_telemetry = tele
                    # auto-record every armed period as a flight
                    if tele.get("armed") and not r.rec.active:
                        r.rec.start("armed")
                        await broadcast_recording(r)
                    elif not tele.get("armed") and r.rec.active and not r.rec.manual:
                        r.rec.stop()
                        await broadcast_recording(r)
                    r.rec.on_telemetry(tele, text)
                elif mtype in ("status", "ack"):
                    r.recent_events.append(text)
                    r.rec.on_event(text)
                await broadcast_text(r, text)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("drone %s socket error: %s", drone_id, e)
    finally:
        if r.drone is ws:
            r.drone = None
            log.info("drone %s disconnected", drone_id)
            if r.rec.active:
                r.rec.on_event(json.dumps({"type": "status", "severity": 3, "text": "aircraft link to relay lost", "t": time.time()}))
                r.rec.stop()
                await broadcast_recording(r)
            await broadcast_text(r, json.dumps({"type": "drone_offline", "id": drone_id}))


# --------------------------------------------------------------------------- #
# Dashboard side
# --------------------------------------------------------------------------- #
@app.websocket("/ws/client/{drone_id}")
async def ws_client(ws: WebSocket, drone_id: str, token: str | None = Query(default=None)):
    who = _principal(token)
    if who is None:
        await ws.close(code=4401)
        return
    if not auth.drone_allowed(who, drone_id):
        await ws.close(code=4403)
        return
    can_command = who["role"] == "operator"
    await ws.accept()
    r = room(drone_id)
    r.clients.add(ws)
    log.info("client %s (%s) joined %s (%d clients)", who["user"], who["role"], drone_id, len(r.clients))
    await _send_json_safe(ws, {"type": "session", "user": who["user"], "role": who["role"], "drones": who["drones"]})
    await _send_json_safe(ws, {"type": "recording", **r.rec.status()})

    await _send_json_safe(ws, {"type": "drone_online" if r.drone else "drone_offline", "id": drone_id})
    if r.last_telemetry:
        await _send_json_safe(ws, r.last_telemetry)
    for text in list(r.recent_events):
        try:
            await ws.send_text(text)
        except Exception:
            break

    try:
        while True:
            text = await ws.receive_text()
            try:
                payload = json.loads(text)
            except Exception:
                continue
            t = payload.get("type")
            if t == "ping":
                # Server-only RTT. Full drone RTT uses type "dping" relayed to the drone.
                await _send_json_safe(ws, {"type": "pong", "t": payload.get("t"), "server_time": time.time()})
                continue
            if t == "cmd" and not can_command:
                await _send_json_safe(ws, {"type": "error", "text": "view-only account: command ignored"})
                continue
            if r.drone is None:
                await _send_json_safe(ws, {"type": "error", "text": "drone offline; command dropped"})
                continue
            if t == "cmd":
                r.rec.on_command(who["user"], payload)
            try:
                await r.drone.send_text(text)
            except Exception:
                await _send_json_safe(ws, {"type": "error", "text": "failed to forward to drone"})
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("client socket error: %s", e)
    finally:
        r.clients.discard(ws)
        log.info("client left %s (%d clients)", drone_id, len(r.clients))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def no_cache_shell(request: Request, call_next):
    """The dashboard is tiny; never let browsers serve a stale copy after a redeploy."""
    resp = await call_next(request)
    if not request.url.path.startswith("/icons/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.get("/api/health")
async def health():
    return {"ok": True, "time": time.time()}


@app.get("/api/drones")
async def list_drones(token: str | None = Query(default=None)):
    who = _principal(token)
    if who is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"drones": [r.status() for r in rooms.values() if auth.drone_allowed(who, r.drone_id)]}


@app.post("/api/login")
async def api_login(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    res = auth.login(str(body.get("username", "")).strip(), str(body.get("password", "")), TOKEN)
    if res is None:
        return JSONResponse({"error": "invalid username or password"}, status_code=401)
    tok, who, exp = res
    log.info("login %s (%s)", who["user"], who["role"])
    return {"token": tok, "expires": exp, **who}


# ---- flight logs ----------------------------------------------------------- #
def _flight_access(token: str | None, drone_id: str) -> dict | JSONResponse:
    who = _principal(token)
    if who is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not auth.drone_allowed(who, drone_id):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return who


@app.get("/api/flights")
async def api_flights(token: str | None = Query(default=None), drone: str | None = Query(default=None)):
    who = _principal(token)
    if who is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    flights = [f for f in rec.list_flights(LOG_DIR, drone) if auth.drone_allowed(who, f.get("drone_id", ""))]
    return {"flights": flights}


@app.get("/api/flights/{drone_id}/{flight_id}")
async def api_flight(drone_id: str, flight_id: str, token: str | None = Query(default=None)):
    who = _flight_access(token, drone_id)
    if isinstance(who, JSONResponse):
        return who
    d = rec.flight_dir(LOG_DIR, drone_id, flight_id)
    if not d:
        return JSONResponse({"error": "not found"}, status_code=404)
    summary = json.loads((d / "summary.json").read_text()) if (d / "summary.json").exists() else {}
    summary["files"] = sorted(f.name for f in d.iterdir() if f.is_file())
    return summary


@app.get("/api/flights/{drone_id}/{flight_id}/track")
async def api_flight_track(drone_id: str, flight_id: str, token: str | None = Query(default=None)):
    who = _flight_access(token, drone_id)
    if isinstance(who, JSONResponse):
        return who
    d = rec.flight_dir(LOG_DIR, drone_id, flight_id)
    if not d:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"points": await asyncio.to_thread(rec.track, d)}


@app.get("/api/flights/{drone_id}/{flight_id}/{filename}")
async def api_flight_file(drone_id: str, flight_id: str, filename: str, token: str | None = Query(default=None)):
    who = _flight_access(token, drone_id)
    if isinstance(who, JSONResponse):
        return who
    d = rec.flight_dir(LOG_DIR, drone_id, flight_id)
    if not d or filename not in ("telemetry.jsonl", "events.jsonl", "video.mp4", "video.mjpeg", "summary.json"):
        return JSONResponse({"error": "not found"}, status_code=404)
    f = d / filename
    if not f.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    media = {"video.mp4": "video/mp4", "video.mjpeg": "video/x-motion-jpeg", "summary.json": "application/json"}.get(filename, "application/x-ndjson")
    return FileResponse(f, media_type=media, filename=f"{drone_id}-{flight_id}-{filename}" if filename != "video.mp4" else None)


@app.delete("/api/flights/{drone_id}/{flight_id}")
async def api_flight_delete(drone_id: str, flight_id: str, token: str | None = Query(default=None)):
    who = _flight_access(token, drone_id)
    if isinstance(who, JSONResponse):
        return who
    if who["role"] != "operator":
        return JSONResponse({"error": "operators only"}, status_code=403)
    d = rec.flight_dir(LOG_DIR, drone_id, flight_id)
    if not d:
        return JSONResponse({"error": "not found"}, status_code=404)
    r = rooms.get(drone_id)
    if r and r.rec.active and r.rec.flight_id == flight_id:
        return JSONResponse({"error": "flight is still recording"}, status_code=409)
    rec.delete_flight(d)
    log.info("%s deleted flight %s/%s", who["user"], drone_id, flight_id)
    return {"ok": True}


@app.post("/api/record/{drone_id}")
async def api_record(drone_id: str, req: Request, token: str | None = Query(default=None)):
    """Manual recording start/stop (operators). Armed periods are recorded automatically anyway."""
    who = _flight_access(token, drone_id)
    if isinstance(who, JSONResponse):
        return who
    if who["role"] != "operator":
        return JSONResponse({"error": "operators only"}, status_code=403)
    body = await req.json()
    r = room(drone_id)
    if body.get("action") == "start":
        if r.drone is None:
            return JSONResponse({"error": "aircraft offline"}, status_code=409)
        r.rec.start("manual", who["user"])
    elif body.get("action") == "stop":
        r.rec.stop()
    else:
        return JSONResponse({"error": "action must be start or stop"}, status_code=400)
    await broadcast_recording(r)
    return r.rec.status()


@app.get("/api/me")
async def api_me(token: str | None = Query(default=None)):
    who = _principal(token)
    if who is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return who


@app.api_route("/", methods=["GET", "HEAD"])
async def index():
    return FileResponse(DASHBOARD_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR)), name="dashboard")
