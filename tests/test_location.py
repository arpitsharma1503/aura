import json
import time

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from conftest import (
    GEOCODE_OK,
    IPAPI_OK,
    IPINFO_OK,
    FakeNetwork,
    make_client,
    make_settings,
    save_location,
)

IP_HOSTS = {"ipinfo.io", "ipapi.co"}


def ip_network(ipinfo=(200, IPINFO_OK), ipapi=(200, IPAPI_OK), **extra) -> FakeNetwork:
    return FakeNetwork({"ipinfo.io": ipinfo, "ipapi.co": ipapi, **extra})


def detect(client: TestClient):
    return client.post("/api/location/detect")


# --- detection ---------------------------------------------------------

def test_detection_saves_approximate_location(tmp_path):
    client = make_client(tmp_path, network=ip_network())
    res = detect(client)
    assert res.status_code == 200
    loc = res.json()
    assert loc["status"] == "configured"
    assert loc["city"] == "Bengaluru"
    assert loc["country"] == "India"           # full name taken from the second provider
    assert loc["source"] == "ip"
    assert loc["label"] == "Approximate"
    assert loc["confirmed"] is False
    assert loc["note"] is None                 # providers agree
    saved = json.loads((tmp_path / "location.json").read_text())["location"]
    assert saved["latitude"] == 12.97 and saved["longitude"] == 77.59  # rounded to ~1 km


def test_public_api_never_exposes_coordinates(tmp_path):
    client = make_client(tmp_path, network=ip_network())
    detect(client)
    for path in ("/api/location", "/api/config"):
        text = client.get(path).text
        assert "latitude" not in text and "longitude" not in text
        assert "12.97" not in text and "77.59" not in text


def test_disagreeing_providers_are_flagged(tmp_path):
    delhi = {**IPAPI_OK, "city": "Delhi", "region": "Delhi", "latitude": 28.67, "longitude": 77.22}
    client = make_client(tmp_path, network=ip_network(ipapi=(200, delhi)))
    loc = detect(client).json()
    assert loc["city"] == "Bengaluru"
    assert "Bengaluru" in loc["note"] and "Delhi" in loc["note"]


def test_fallback_provider_used_when_primary_fails(tmp_path):
    client = make_client(tmp_path, network=ip_network(ipinfo=(429, {"error": "rate limited"})))
    loc = detect(client).json()
    assert loc["city"] == "Bengaluru"
    assert loc["provider"] == "ipapi.co"


def test_ipapi_error_payload_is_treated_as_failure(tmp_path):
    client = make_client(tmp_path, network=ip_network(
        ipinfo=httpx.ConnectTimeout("timeout"), ipapi=(200, {"error": True, "reason": "RateLimited"})))
    res = detect(client)
    assert res.status_code == 502


def test_detection_failure_leaves_location_unconfigured(tmp_path):
    client = make_client(tmp_path)  # all network calls fail
    res = detect(client)
    assert res.status_code == 502
    loc = client.get("/api/location").json()
    assert loc["status"] == "unconfigured"
    assert loc["city"] is None                 # never silently falls back to a city
    assert loc["detection_error"]


def test_failed_redetect_keeps_existing_location(tmp_path):
    save_location(tmp_path)
    client = make_client(tmp_path)  # network down
    assert detect(client).status_code == 502
    assert client.get("/api/location").json()["city"] == "Pune"


def test_redetect_cooldown(tmp_path):
    client = make_client(tmp_path, network=ip_network(), location_detect_cooldown_seconds=60)
    assert detect(client).status_code == 200
    assert detect(client).status_code == 429


# --- startup behaviour & persistence -------------------------------------

def test_startup_detects_once_when_unconfigured(tmp_path):
    net = ip_network()
    app = create_app(make_settings(tmp_path), transport=net.transport)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1)) as client:
        # Background task; wait for it to finish.
        for _ in range(50):
            if client.get("/api/location").json()["status"] == "configured":
                break
            time.sleep(0.02)
        assert client.get("/api/location").json()["city"] == "Bengaluru"
    assert set(net.hosts()) == IP_HOSTS


def test_saved_location_is_not_redetected_on_restart(tmp_path):
    save_location(tmp_path)
    net = ip_network()
    app = create_app(make_settings(tmp_path), transport=net.transport)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1)) as client:
        assert client.get("/api/location").json()["city"] == "Pune"
    assert net.calls == []  # no external request at all


def test_detection_off_makes_no_requests(tmp_path):
    net = ip_network()
    app = create_app(make_settings(tmp_path, location_detection="off"), transport=net.transport)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1)) as client:
        assert client.get("/api/location").json()["status"] == "unconfigured"
    assert net.calls == []


def test_location_persists_across_app_instances(tmp_path):
    detect(make_client(tmp_path, network=ip_network()))
    again = make_client(tmp_path)  # fresh app, network down
    loc = again.get("/api/location").json()
    assert loc["city"] == "Bengaluru" and loc["source"] == "ip"


def test_corrupt_location_file_is_unconfigured(tmp_path):
    (tmp_path / "location.json").write_text("{nope")
    assert make_client(tmp_path).get("/api/location").json()["status"] == "unconfigured"


# --- confirmation & manual choice ------------------------------------------

def test_confirm_detected_location(tmp_path):
    delhi = {**IPAPI_OK, "city": "Delhi"}
    client = make_client(tmp_path, network=ip_network(ipapi=(200, delhi)))
    detect(client)
    loc = client.post("/api/location/confirm").json()
    assert loc["confirmed"] is True
    assert loc["label"] == "Confirmed"
    assert loc["note"] is None


def test_confirm_without_location_conflicts(client):
    assert client.post("/api/location/confirm").status_code == 409


def test_search_and_choose_city(tmp_path):
    net = FakeNetwork({"geocoding-api.open-meteo.com": (200, GEOCODE_OK)})
    client = make_client(tmp_path, network=net)
    results = client.get("/api/location/search", params={"q": "Pune"}).json()
    assert [r["country"] for r in results] == ["India", "Elsewhere"]
    assert "latitude" not in results[0]        # coordinates stay on the server

    loc = client.put("/api/location", json={"id": results[0]["id"]}).json()
    assert loc["city"] == "Pune"
    assert loc["source"] == "manual"
    assert loc["label"] == "Set manually"
    assert loc["timezone"] == "Asia/Kolkata"
    saved = json.loads((tmp_path / "location.json").read_text())["location"]
    assert (saved["latitude"], saved["longitude"]) == (18.52, 73.86)
    assert client.get("/api/config").json()["timezone"] == "Asia/Kolkata"


def test_search_results_are_cached(tmp_path):
    net = FakeNetwork({"geocoding-api.open-meteo.com": (200, GEOCODE_OK)})
    client = make_client(tmp_path, network=net)
    client.get("/api/location/search", params={"q": "Pune"})
    client.get("/api/location/search", params={"q": "pune "})
    assert len(net.calls) == 1


def test_choose_unknown_result_is_404(client):
    assert client.put("/api/location", json={"id": 12345}).status_code == 404


def test_search_unavailable_is_502(client):
    assert client.get("/api/location/search", params={"q": "Pune"}).status_code == 502


def test_search_query_validation(client):
    assert client.get("/api/location/search", params={"q": "x"}).status_code == 422


def test_ipinfo_token_is_server_side_only(tmp_path):
    seen = {}

    def ipinfo(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=IPINFO_OK)

    net = ip_network(ipinfo=ipinfo)
    settings = Settings(_env_file=None, data_dir=tmp_path, log_dir=tmp_path, timezone="UTC",
                        weather_provider="none", ipinfo_token="secret-token")
    app = create_app(settings, transport=net.transport)
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1))
    client.post("/api/location/detect")
    assert seen["auth"] == "Bearer secret-token"
    for path in ("/api/config", "/api/location", "/"):
        assert "secret-token" not in client.get(path).text
