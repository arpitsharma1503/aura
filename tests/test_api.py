from conftest import make_client, save_location


def test_health_ok(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["time"].endswith("+05:30")
    assert res.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in res.headers["content-security-policy"]


def test_index_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "AURA" in res.text


def test_api_docs_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_config_has_no_default_city(client):
    body = client.get("/api/config").json()
    assert body["location"]["status"] == "unconfigured"
    assert body["location"]["city"] is None
    assert "mumbai" not in client.get("/api/config").text.lower()
    assert body["dev_preview"] is False


def test_config_uses_location_timezone(tmp_path):
    save_location(tmp_path, timezone="Europe/London", city="London")
    body = make_client(tmp_path).get("/api/config").json()
    assert body["timezone"] == "Europe/London"
    assert body["location"]["city"] == "London"


def test_weather_disabled(client):
    body = client.get("/api/weather").json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "disabled"
    assert body["data"] is None
    assert body["recommendations"] == []


def test_weather_needs_location(tmp_path):
    client = make_client(tmp_path, weather_provider="open_meteo")
    body = client.get("/api/weather").json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "no_location"
    assert body["data"] is None


def test_mock_weather_is_labelled(tmp_path):
    save_location(tmp_path)
    client = make_client(tmp_path, weather_provider="mock")
    body = client.get("/api/weather").json()
    assert body["status"] == "ok"
    assert body["is_mock"] is True
    assert body["data"]["is_mock"] is True
    assert "sample" in body["data"]["source"].lower()
    assert body["recommendations"]
    assert client.get("/api/config").json()["dev_preview"] is True
    # Sample data must never be written to the live-weather cache.
    assert not (tmp_path / "weather_cache.json").exists()


# --- network restrictions ---

def test_public_client_rejected(tmp_path):
    client = make_client(tmp_path, client_ip="8.8.8.8")
    assert client.get("/api/health").status_code == 403


def test_public_ipv6_client_rejected(tmp_path):
    client = make_client(tmp_path, client_ip="2001:4860:4860::8888")
    assert client.get("/api/health").status_code == 403


def test_loopback_and_lan_clients_allowed(tmp_path):
    for ip in ("127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.50", "fd12::1", "fe80::1"):
        assert make_client(tmp_path, client_ip=ip).get("/api/health").status_code == 200, ip


def test_unknown_host_header_rejected(client):
    res = client.get("/api/health", headers={"host": "evil.example.com"})
    assert res.status_code == 400


def test_private_ip_host_header_allowed(client):
    res = client.get("/api/health", headers={"host": "192.168.1.11:8080"})
    assert res.status_code == 200



def test_config_has_build_id_that_changes_on_restart(tmp_path):
    first = make_client(tmp_path).get("/api/config").json()["build"]
    second = make_client(tmp_path).get("/api/config").json()["build"]
    assert first and second and first != second
