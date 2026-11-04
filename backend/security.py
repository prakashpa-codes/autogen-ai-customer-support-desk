"""Staff authentication and public-endpoint rate limiting (standard library only).

* Passwords are stored as PBKDF2-SHA256 hashes (DESK_PASSWORD_HASH), never in plain text.
  Generate one with:  python scripts/hash_password.py
* Sessions are short-lived HMAC-signed tokens (stateless, so they survive worker restarts
  as long as SECRET_KEY is stable).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque

PBKDF2_ITERATIONS = 310_000


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256:{iterations}:{_b64e(salt)}:{_b64e(digest)}"  # no "$": safe in .env and compose files


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split(":")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _b64d(salt), int(iterations))
        return hmac.compare_digest(_b64e(digest), expected)
    except (ValueError, TypeError):
        return False


def create_token(username: str, secret: str, ttl_seconds: int) -> tuple[str, int]:
    expires = int(time.time()) + ttl_seconds
    payload = _b64e(json.dumps({"sub": username, "exp": expires}).encode())
    signature = _b64e(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}", expires


def verify_token(token: str, secret: str) -> str | None:
    try:
        payload, signature = token.split(".")
        expected = _b64e(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_b64d(payload))
        return data["sub"] if data["exp"] > time.time() else None
    except (ValueError, KeyError, TypeError):
        return None


class RateLimiter:
    """Sliding-window limiter keyed by client IP (in memory; one backend process)."""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True
