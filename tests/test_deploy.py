"""Startup configuration: systemd user unit, kiosk launcher, installer, docs."""

import configparser
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy" / "systemd" / "aura.service"
KIOSK = ROOT / "deploy" / "kiosk" / "aura-kiosk.sh"
DESKTOP = ROOT / "deploy" / "kiosk" / "aura-kiosk.desktop"
INSTALL = ROOT / "deploy" / "install.sh"
UNINSTALL = ROOT / "deploy" / "uninstall.sh"
README = ROOT / "README.md"


def unit():
    cp = configparser.ConfigParser(strict=False, interpolation=None, comment_prefixes=("#", ";"))
    cp.optionxform = str
    cp.read(UNIT)
    return cp


def test_unit_runs_the_real_entry_point_from_the_venv():
    svc = unit()["Service"]
    assert svc["ExecStart"] == f"{ROOT}/.venv/bin/python -m app"
    assert (ROOT / ".venv" / "bin" / "python").exists()
    assert (ROOT / "app" / "__main__.py").exists()
    assert svc["WorkingDirectory"] == str(ROOT)
    assert svc["Type"] == "exec"


def test_unit_restart_policy_and_hardening():
    cp = unit()
    svc = cp["Service"]
    assert svc["Restart"] == "on-failure"
    assert int(svc["RestartSec"]) >= 1
    assert int(cp["Unit"]["StartLimitBurst"]) >= 1 and int(cp["Unit"]["StartLimitIntervalSec"]) > 0
    assert svc["UMask"] == "0077" and svc["NoNewPrivileges"] == "yes"
    assert cp["Install"]["WantedBy"] == "default.target"     # user manager; starts at boot with linger


def test_unit_is_unprivileged_and_has_no_secrets():
    text = UNIT.read_text()
    assert "User=root" not in text and "sudo" not in text
    assert not re.search(r"^\s*(User|Group)=", text, re.M)   # user unit: runs as the owner
    assert "EnvironmentFile" not in text                       # app reads .env itself
    env_lines = [l for l in text.splitlines() if l.startswith("Environment=")]
    assert env_lines == ["Environment=PYTHONUNBUFFERED=1"]
    for secret in ("TOMTOM", "API_KEY", "PIN", "13.0087", "77.7381"):
        assert secret not in text


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not available")
def test_unit_passes_systemd_verify():
    r = subprocess.run(["systemd-analyze", "--user", "verify", str(UNIT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", [KIOSK, INSTALL, UNINSTALL])
def test_scripts_are_valid_and_executable(script):
    assert os.access(script, os.X_OK), f"{script} not executable"
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_kiosk_launcher_configuration():
    text = KIOSK.read_text()
    port = Settings(_env_file=None).port
    assert f"http://127.0.0.1:{port}/" in text                 # local dashboard (trusted display)
    assert "/api/health" in text and "until backend_up" in text # waits for the backend
    assert "--kiosk" in text and "--noerrdialogs" in text and "--disable-session-crashed-bubble" in text
    assert "--user-data-dir=" in text                          # own profile, not the desktop browser's
    assert "flock -n 9" in text                                # single instance
    # Children must not inherit the lock (regression: leftover sleep kept it).
    code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    for cmd in ("curl", "sleep 2", "sleep 3", "sleep 10", "--check-for-update-interval"):
        line = next(l for l in code if cmd in l)
        assert "9>&-" in line, line
    assert "while true" in text and "restarting in 3 s" in text  # relaunch after exit/crash
    assert "/sys/class/drm" in text                            # only with a screen attached


def test_desktop_autostart_entry():
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(DESKTOP)
    entry = cp["Desktop Entry"]
    assert entry["Type"] == "Application"
    assert entry["Exec"] == str(KIOSK)
    if shutil.which("desktop-file-validate"):
        r = subprocess.run(["desktop-file-validate", str(DESKTOP)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_installer_is_user_level_and_backs_up():
    text = INSTALL.read_text()
    code = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith(("#", "echo"))]
    assert not [l for l in code if re.search(r"(^|[;&|]\s*)sudo\b", l)], "installer must not run sudo"
    assert "systemctl --user" in text and ".bak-" in text and "--takeover" in text
    assert "/etc/" not in text


INSTALLED_UNIT = Path.home() / ".config/systemd/user/aura.service"
INSTALLED_DESKTOP = Path.home() / ".config/autostart/aura-kiosk.desktop"


@pytest.mark.skipif(not INSTALLED_UNIT.exists(), reason="service not installed on this machine")
def test_installed_copies_match_the_repo():
    assert INSTALLED_UNIT.read_text() == UNIT.read_text()
    if INSTALLED_DESKTOP.exists():
        assert INSTALLED_DESKTOP.read_text() == DESKTOP.read_text()


def test_readme_documents_operations():
    text = README.read_text()
    for cmd in ("systemctl --user status aura", "systemctl --user restart aura", "systemctl --user stop aura",
                "systemctl --user start aura", "journalctl --user-unit aura", "deploy/install.sh",
                "deploy/uninstall.sh", "aura-kiosk.log", "power"):
        assert cmd in text, cmd
