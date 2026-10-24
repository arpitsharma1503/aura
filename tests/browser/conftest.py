"""Fixtures for real-browser tests: a headless Chromium and throwaway AURA servers.

Each server runs the real app (`python -m app`) on a free localhost port with
its own temporary data directory, so tests never touch the household's data.
Skipped automatically if Chromium isn't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from cdp import Browser  # noqa: E402

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
pytestmark = pytest.mark.browser


def pytest_collection_modifyitems(config, items):
    for item in items:
        if HERE in Path(item.fspath).parents:
            item.add_marker(pytest.mark.browser)
            if not CHROMIUM:
                item.add_marker(pytest.mark.skip(reason="Chromium not installed"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_url(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


class AuraServer:
    def __init__(self, data_dir: Path, env: dict | None = None) -> None:
        self.data_dir = data_dir
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {
            **os.environ,
            "AURA_HOST": "127.0.0.1",
            "AURA_PORT": str(self.port),
            "AURA_DATA_DIR": str(data_dir),
            "AURA_LOG_DIR": str(data_dir),
            "AURA_WEATHER_PROVIDER": "none",
            "AURA_LOCATION_DETECTION": "off",
            "AURA_COMMUTE_ENABLED": "false",
            "AURA_CALENDAR_FEEDS": "", "AURA_VOICE_PORT": "1025",          # never fetch the household's real calendars in tests
            # never connect to the household's real Home Assistant (the token in .env)
            "AURA_HOMEASSISTANT_TOKEN": "", "AURA_HOMEASSISTANT_URL": "http://127.0.0.1:1",
            "AURA_EDIT_ACCESS": "lan",
            "AURA_PUBLIC_URL": f"http://127.0.0.1:{self.port}",
            **(env or {}),
        }
        self.proc = None
        self.start()

    def start(self) -> None:
        self.log = open(self.data_dir / "server.out", "a")  # noqa: SIM115
        self.proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=self.env,
                                     stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        wait_url(f"{self.url}/api/health")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()

    def restart(self) -> None:
        self.stop()
        self.start()

    def api(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.url}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


@pytest.fixture(scope="session")
def chrome():
    port = free_port()
    profile = tempfile.mkdtemp(prefix="aura-chrome-")
    proc = subprocess.Popen([
        CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--password-store=basic",
        "--no-first-run", "--disable-extensions", "--hide-scrollbars", "--mute-audio",
        f"--remote-debugging-port={port}", f"--user-data-dir={profile}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    wait_url(f"http://127.0.0.1:{port}/json/version")
    browser = Browser(port)
    yield browser
    browser.close()
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(timeout=10)
    shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture(autouse=True)
def _close_pages(request):
    """Close tabs after each test so stale pages can't keep polling dead servers."""
    yield
    if "chrome" in request.fixturenames:
        request.getfixturevalue("chrome").close_pages()


@pytest.fixture
def server(tmp_path):
    srv = AuraServer(tmp_path)
    yield srv
    srv.stop()


@pytest.fixture
def make_server(tmp_path):
    made = []

    def factory(**env):
        d = tmp_path / f"srv{len(made)}"
        d.mkdir()
        srv = AuraServer(d, {k: str(v) for k, v in env.items()})
        made.append(srv)
        return srv

    yield factory
    for s in made:
        s.stop()


ARTIFACTS = ROOT / "tests" / "browser" / "artifacts"


@pytest.fixture(scope="session")
def artifacts():
    ARTIFACTS.mkdir(exist_ok=True)
    return ARTIFACTS
