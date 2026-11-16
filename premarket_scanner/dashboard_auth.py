"""Password and sessions for the dashboard's remote port (the one behind the tunnel).

The password is never stored: ``data/dashboard_auth.json`` keeps a salted PBKDF2 hash, written by
``dashboard_password.py``. Sessions live in memory, so every restart signs everyone out. Five wrong
passwords in a row lock the login for 15 minutes, for everybody: the page has one owner.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

ITERATIONS = 310_000
MIN_LENGTH = 10
SESSION_S = 12 * 3600
MAX_FAILURES = 5
LOCK_S = 15 * 60
COOKIE = "gt_session"


def auth_file() -> Path:
    return config.DATA_DIR / "dashboard_auth.json"


def _hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def set_password(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"the password needs at least {MIN_LENGTH} characters")
    salt = os.urandom(16)
    data = {"salt": salt.hex(), "hash": _hash(password, salt, ITERATIONS).hex(), "iterations": ITERATIONS}
    path = auth_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _stored() -> dict | None:
    try:
        data = json.loads(auth_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("hash") and data.get("salt") else None
    except (OSError, ValueError):
        return None


def has_password() -> bool:
    return _stored() is not None


def verify(password: str) -> bool:
    data = _stored()
    if data is None:
        return False
    try:
        want = bytes.fromhex(data["hash"])
        got = _hash(password, bytes.fromhex(data["salt"]), int(data.get("iterations") or ITERATIONS))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(want, got)


class Sessions:
    """Who is signed in, and the lock after repeated wrong passwords."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}
        self._failures = 0
        self._locked_until = 0.0

    def locked_for(self) -> int:
        """Seconds until a login is accepted again (0 = not locked)."""
        with self._lock:
            return max(0, int(self._locked_until - time.monotonic()))

    def login(self, password: str) -> str | None:
        """A new session token for the right password, else None. Counts failures."""
        if self.locked_for():
            return None
        ok = verify(password)
        with self._lock:
            if not ok:
                self._failures += 1
                if self._failures >= MAX_FAILURES:
                    self._failures = 0
                    self._locked_until = time.monotonic() + LOCK_S
                return None
            self._failures = 0
            now = time.monotonic()
            self._tokens = {t: exp for t, exp in self._tokens.items() if exp > now}
            token = secrets.token_urlsafe(32)
            self._tokens[token] = now + SESSION_S
            return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return self._tokens.get(token, 0.0) > time.monotonic()

    def logout(self, token: str | None) -> None:
        with self._lock:
            self._tokens.pop(token or "", None)
