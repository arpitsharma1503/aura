"""Household access control for changes (groceries, location settings).

* Reading the dashboard and list is allowed for anyone on the home network.
* With ``AURA_EDIT_ACCESS=lan`` (default) any device on the home network may
  also make changes; the LAN-only and same-origin checks in ``security.py``
  still apply.
* With ``AURA_EDIT_ACCESS=pin`` changes require either:
  - a request from the Pi itself (loopback), i.e. the entrance display, or
  - a signed session cookie, obtained once per phone by entering the
    household PIN (``AURA_HOUSEHOLD_PIN`` in ``.env``).
* Sessions are HMAC-signed with a random key kept in ``data/session.key``
  *and* the PIN, so changing the PIN signs every device out.
* Wrong PINs are rate-limited per device and household-wide.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import HTTPException, Request

from .security import is_loopback

COOKIE_NAME = "aura_session"


class LoginLocked(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Too many wrong PIN attempts")
        self.retry_after = retry_after


class AccessControl:
    def __init__(
        self,
        pin: str | None,
        key_path: Path,
        *,
        session_days: int = 365,
        trust_localhost: bool = True,
        open_lan: bool = False,
        max_failures: int = 5,
        household_max_failures: int = 20,
        window_seconds: int = 600,
        clock=time.time,
    ) -> None:
        self.pin = pin or None
        self.session_seconds = session_days * 86400
        self.trust_localhost = trust_localhost
        self.open_lan = open_lan
        self.max_failures = max_failures
        self.household_max_failures = household_max_failures
        self.window = window_seconds
        self.clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._all_failures: deque[float] = deque()
        secret = self._load_secret(key_path)
        self._key = hmac.new(secret, f"pin:{self.pin or ''}".encode(), hashlib.sha256).digest()

    @staticmethod
    def _load_secret(path: Path) -> bytes:
        try:
            data = bytes.fromhex(path.read_text().strip())
            if len(data) >= 32:
                return data
        except (OSError, ValueError):
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        data = secrets.token_bytes(32)
        path.write_text(data.hex())
        path.chmod(0o600)
        return data

    @property
    def pin_configured(self) -> bool:
        return self.pin is not None

    # ----- sessions -----

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        payload = f"v1.{int(self.clock())}.{secrets.token_hex(8)}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str | None) -> bool:
        if not token or not self.pin_configured:
            return False
        try:
            version, ts, nonce, sig = token.split(".")
            issued = int(ts)
        except ValueError:
            return False
        if version != "v1" or not hmac.compare_digest(sig, self._sign(f"{version}.{ts}.{nonce}")):
            return False
        return 0 <= self.clock() - issued < self.session_seconds

    # ----- PIN login -----

    def _prune(self, q: deque[float], now: float) -> None:
        while q and now - q[0] > self.window:
            q.popleft()

    def check_pin(self, client: str, pin: str) -> bool:
        now = self.clock()
        mine = self._failures[client]
        self._prune(mine, now)
        self._prune(self._all_failures, now)
        if len(mine) >= self.max_failures:
            raise LoginLocked(int(self.window - (now - mine[0])) + 1)
        if len(self._all_failures) >= self.household_max_failures:
            raise LoginLocked(int(self.window - (now - self._all_failures[0])) + 1)
        ok = self.pin_configured and hmac.compare_digest(pin.encode(), self.pin.encode())
        if ok:
            mine.clear()
        else:
            mine.append(now)
            self._all_failures.append(now)
        return ok

    # ----- request checks -----

    def is_local(self, request: Request) -> bool:
        client = request.client.host if request.client else None
        return self.trust_localhost and is_loopback(client)

    def can_edit(self, request: Request) -> bool:
        # In "lan" mode the middleware has already limited clients to the home network.
        return self.open_lan or self.is_local(request) or self.verify(request.cookies.get(COOKIE_NAME))

    def method(self, request: Request) -> str | None:
        if self.open_lan:
            return "lan"
        if self.is_local(request):
            return "local"
        if self.verify(request.cookies.get(COOKIE_NAME)):
            return "pin"
        return None

    def require_editor(self, request: Request) -> None:
        if not self.can_edit(request):
            raise HTTPException(
                status_code=401,
                detail="Enter the household PIN on this device to make changes",
            )
