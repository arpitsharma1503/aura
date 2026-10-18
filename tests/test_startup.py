"""Backend startup/shutdown behaviour (regressions found in the Phase 3 audit)."""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from app.__main__ import port_in_use

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(tmp_path, port, extra_env=None):
    env = {
        **os.environ,
        "AURA_PORT": str(port), "AURA_HOST": "127.0.0.1", "AURA_DATA_DIR": str(tmp_path),
        "AURA_LOG_DIR": str(tmp_path), "AURA_WEATHER_PROVIDER": "none", "AURA_LOCATION_DETECTION": "off",
        "AURA_COMMUTE_ENABLED": "false", "AURA_CALENDAR_FEEDS": "",
        **(extra_env or {}),
    }
    return subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_healthy(port, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                return r.status == 200
        except OSError:
            time.sleep(0.1)
    return False


def test_port_in_use_detection():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert port_in_use("127.0.0.1", s.getsockname()[1]) is True
    assert port_in_use("127.0.0.1", free_port()) is False


@pytest.mark.timeout_sensitive
def test_server_starts_serves_and_stops_quickly_with_open_stream(tmp_path):
    port = free_port()
    proc = start(tmp_path, port)
    try:
        assert wait_healthy(port), proc.stdout.read() if proc.poll() is not None else "not healthy"
        # Hold a live-update stream open, like a dashboard would.
        stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/events", timeout=5)
        assert stream.readline().startswith(b"retry:")
        t0 = time.monotonic()
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=10)
        elapsed = time.monotonic() - t0
    finally:
        if proc.poll() is None:
            proc.kill()
    # uvicorn re-raises the signal after a clean shutdown, so -SIGINT is normal.
    assert proc.returncode in (0, -signal.SIGINT), out
    assert "Application shutdown complete" in out
    assert elapsed < 2.5, f"shutdown took {elapsed:.1f}s"
    assert "timeout graceful shutdown exceeded" not in out
    # Files created by the server are private to this user.
    for f in tmp_path.glob("aura.db*"):
        assert oct(f.stat().st_mode & 0o777) == "0o600", f


def test_second_instance_exits_with_clear_message(tmp_path):
    port = free_port()
    first = start(tmp_path, port)
    try:
        assert wait_healthy(port)
        second = start(tmp_path / "x", port)
        out, _ = second.communicate(timeout=15)
        assert second.returncode == 1
        assert "already running on port" in out
        assert "Traceback" not in out
    finally:
        first.send_signal(signal.SIGINT)
        first.communicate(timeout=10)


def test_existing_world_readable_db_files_are_made_private(tmp_path):
    """Regression: WAL/SHM files created before the private umask stayed 644."""
    from app.groceries import GroceryStore
    db = tmp_path / "aura.db"
    GroceryStore(db).close()
    for suffix in ("", "-wal", "-shm"):
        f = db.with_name(db.name + suffix)
        f.touch()
        f.chmod(0o644)
    store = GroceryStore(db)
    for suffix in ("", "-wal", "-shm"):
        assert oct(db.with_name(db.name + suffix).stat().st_mode & 0o777) == "0o600"
    store.close()
