"""
User accounts and sessions for the relay.

Users live in a JSON file (VAYUVEER_USERS_FILE, default ./users.json):
  {"users": [{"username": "demo", "password": "<pbkdf2 hash>", "role": "operator", "drones": ["demo-01"]}]}

Roles: operator = full control, viewer = telemetry + video only.
drones: list of drone ids the user may open, or ["*"] for all.
Sessions are HMAC-signed tokens (no server-side state), valid SESSION_HOURS.
Manage accounts with:  python manage_users.py add|remove|list
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

USERS_FILE = Path(os.environ.get("VAYUVEER_USERS_FILE", Path(__file__).with_name("users.json")))
SESSION_HOURS = float(os.environ.get("VAYUVEER_SESSION_HOURS", "12"))


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def load_users() -> dict[str, dict]:
    if not USERS_FILE.exists():
        return {}
    data = json.loads(USERS_FILE.read_text())
    return {u["username"]: u for u in data.get("users", [])}


def save_users(users: dict[str, dict]) -> None:
    USERS_FILE.write_text(json.dumps({"users": list(users.values())}, indent=2))


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_session(secret: str, user: dict) -> tuple[str, float]:
    exp = time.time() + SESSION_HOURS * 3600
    payload = {"u": user["username"], "r": user.get("role", "viewer"), "d": user.get("drones", []), "exp": exp}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"s.{body}.{sig}", exp


def verify_session(secret: str, token: str) -> dict | None:
    try:
        prefix, body, sig = token.split(".")
        if prefix != "s":
            return None
        good = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        payload = json.loads(_unb64(body))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def authenticate(password_or_token: str, master: str) -> dict | None:
    """Resolve a credential presented on a WebSocket/REST call to a principal.
    Returns {"user", "role", "drones"} or None."""
    if hmac.compare_digest(password_or_token, master):
        return {"user": "master", "role": "operator", "drones": ["*"]}
    p = verify_session(master, password_or_token)
    if p:
        return {"user": p["u"], "role": p["r"], "drones": p["d"]}
    return None


def login(username: str, password: str, master: str) -> tuple[str, dict, float] | None:
    users = load_users()
    u = users.get(username)
    if not u or not verify_password(password, u["password"]):
        return None
    tok, exp = issue_session(master, u)
    return tok, {"user": u["username"], "role": u.get("role", "viewer"), "drones": u.get("drones", [])}, exp


def drone_allowed(principal: dict, drone_id: str) -> bool:
    d = principal.get("drones", [])
    return "*" in d or drone_id in d
