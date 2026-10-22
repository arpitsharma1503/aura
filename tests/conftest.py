import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

IPINFO_OK = {
    "ip": "203.0.113.7", "city": "Bengaluru", "region": "Karnataka", "country": "IN",
    "loc": "12.9719,77.5937", "timezone": "Asia/Kolkata",
}
IPAPI_OK = {
    "ip": "203.0.113.7", "city": "Bengaluru", "region": "Karnataka", "country": "IN",
    "country_name": "India", "country_code": "IN", "latitude": 12.97194,
    "longitude": 77.59369, "timezone": "Asia/Kolkata",
}
GEOCODE_OK = {"results": [
    {"id": 1259229, "name": "Pune", "latitude": 18.51957, "longitude": 73.85535,
     "country_code": "IN", "timezone": "Asia/Kolkata", "country": "India",
     "admin1": "Maharashtra", "admin2": "Pune"},
    {"id": 999, "name": "Pune", "latitude": 1.0, "longitude": 2.0, "country_code": "XX",
     "timezone": "UTC", "country": "Elsewhere", "admin1": "Somewhere"},
]}


class FakeNetwork:
    """httpx.MockTransport router: map hostnames to handlers, record calls.

    Anything not explicitly routed fails like an unreachable network, so the
    test suite can never touch the real internet.
    """

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        handler = self.routes.get(request.url.host)
        if handler is None:
            raise httpx.ConnectError("network disabled in tests", request=request)
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(request)
        status, body = handler
        return httpx.Response(status, json=body)

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.calls]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "timezone": "Asia/Kolkata",
        "weather_provider": "none",
        "data_dir": tmp_path,
        "log_dir": tmp_path,
        "location_detect_cooldown_seconds": 0,
        "edit_access": "pin",   # most access tests exercise the stricter mode
        "voice_port": 1025,     # never reach a real voice worker running on the Pi
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_client(tmp_path: Path, client_ip: str = "127.0.0.1", network: FakeNetwork | None = None,
                **overrides) -> TestClient:
    network = network or FakeNetwork()
    app = create_app(make_settings(tmp_path, **overrides), transport=network.transport)
    return TestClient(app, base_url="http://localhost", client=(client_ip, 50000))


def save_location(tmp_path: Path, **overrides) -> dict:
    loc = {
        "city": "Pune", "region": "Maharashtra", "country": "India", "country_code": "IN",
        "latitude": 18.52, "longitude": 73.86, "timezone": "Asia/Kolkata",
        "source": "manual", "confirmed": True, "provider": "Open-Meteo geocoding",
        "updated_at": datetime(2026, 10, 18, tzinfo=UTC).isoformat(),
    }
    loc.update(overrides)
    (tmp_path / "location.json").write_text(json.dumps({"location": loc}))
    return loc


@pytest.fixture
def client(tmp_path):
    return make_client(tmp_path)
