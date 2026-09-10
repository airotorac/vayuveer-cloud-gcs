"""
VayuVeer Cloud GCS - relay server.

Sits on the public internet. Drones (RPi 5 agents) connect OUT to it over a
WebSocket, dashboards connect to it too, and the server relays:

  dashboard  --(JSON commands)-->  server  --> drone
  drone      --(JSON telemetry, binary JPEG frames)--> server --> all dashboards

Neither side needs a public IP or port-forwarding, which is what makes it work
over 4G/5G and behind NAT.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
Env:  AX4_TOKEN   shared secret both sides must present (?token=...)
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

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("ax4.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TOKEN = os.environ.get("AX4_TOKEN", "change-me")
DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

app = FastAPI(title="VayuVeer Cloud GCS relay")


class DroneRoom:
    """Everything connected for one drone id."""

    def __init__(self, drone_id: str):
        self.drone_id = drone_id
        self.drone: WebSocket | None = None
        self.clients: Set[WebSocket] = set()
        self.last_telemetry: dict | None = None
        self.recent_events: deque[str] = deque(maxlen=50)   # status/ack replayed to new dashboards
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


def _auth_ok(token: str | None) -> bool:
    return token is not None and token == TOKEN


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
                await broadcast_bytes(r, data)
            elif msg.get("text") is not None:
                text = msg["text"]
                # Cache last telemetry so a freshly opened dashboard gets state immediately.
                if text.startswith('{"type": "telemetry"') or text.startswith('{"type":"telemetry"'):
                    try:
                        r.last_telemetry = json.loads(text)
                    except Exception:
                        pass
                elif text.startswith('{"type": "status"') or text.startswith('{"type": "ack"'):
                    r.recent_events.append(text)
                await broadcast_text(r, text)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("drone %s socket error: %s", drone_id, e)
    finally:
        if r.drone is ws:
            r.drone = None
            log.info("drone %s disconnected", drone_id)
            await broadcast_text(r, json.dumps({"type": "drone_offline", "id": drone_id}))


# --------------------------------------------------------------------------- #
# Dashboard side
# --------------------------------------------------------------------------- #
@app.websocket("/ws/client/{drone_id}")
async def ws_client(ws: WebSocket, drone_id: str, token: str | None = Query(default=None)):
    if not _auth_ok(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    r = room(drone_id)
    r.clients.add(ws)
    log.info("client joined %s (%d clients)", drone_id, len(r.clients))

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
            if r.drone is None:
                await _send_json_safe(ws, {"type": "error", "text": "drone offline; command dropped"})
                continue
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
@app.get("/api/health")
async def health():
    return {"ok": True, "time": time.time()}


@app.get("/api/drones")
async def list_drones(token: str | None = Query(default=None)):
    if not _auth_ok(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"drones": [r.status() for r in rooms.values()]}


@app.get("/")
async def index():
    return FileResponse(DASHBOARD_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR)), name="dashboard")
