"""Launch the dashboard: ``.venv/bin/python -m app``."""

import asyncio
import logging
import os
import socket
import sys
from logging.handlers import RotatingFileHandler

import uvicorn

from .config import get_settings
from .main import create_app

log = logging.getLogger("aura")


def port_in_use(host: str, port: int) -> bool:
    """True if something is already listening on host:port."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((probe_host, port)) == 0


class AuraServer(uvicorn.Server):
    """Closes live-update streams as soon as shutdown starts.

    Without this, uvicorn waits for open Server-Sent Event connections until
    its graceful-shutdown timeout and then logs an error.
    """

    def __init__(self, config: uvicorn.Config, broker) -> None:
        super().__init__(config)
        self.broker = broker
        self._loop: asyncio.AbstractEventLoop | None = None

    async def startup(self, sockets=None) -> None:
        self._loop = asyncio.get_running_loop()
        await super().startup(sockets)

    def handle_exit(self, sig, frame) -> None:
        # Runs in a signal handler: hand the work to the event loop safely.
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.broker.close)
        super().handle_exit(sig, frame)


def main() -> None:
    os.umask(0o077)  # data files (SQLite WAL, caches, keys) readable by this user only
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(settings.log_dir / "aura.log", maxBytes=1_000_000, backupCount=3),
        ],
    )
    # httpx logs full request URLs (including coordinates and API keys) at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if port_in_use(settings.host, settings.port):
        log.error(
            "AURA is already running on port %s (or another program uses it). "
            "Stop it first: kill $(ss -ltnp | grep ':%s ' | grep -o 'pid=[0-9]*' | cut -d= -f2)",
            settings.port, settings.port,
        )
        sys.exit(1)

    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=False,  # no reverse proxy; trust only the socket address
        server_header=False,
        log_config=None,
        timeout_graceful_shutdown=3,  # backstop; streams are closed in handle_exit
    )
    AuraServer(config, app.state.broker).run()


if __name__ == "__main__":
    main()
