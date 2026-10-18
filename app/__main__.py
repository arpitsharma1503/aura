"""Launch the dashboard: ``.venv/bin/python -m app``."""

import logging
from logging.handlers import RotatingFileHandler

import uvicorn

from .config import get_settings
from .main import create_app


def main() -> None:
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
    # httpx logs full request URLs (including home coordinates) at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=False,  # no reverse proxy; trust only the socket address
        server_header=False,
        log_config=None,
        # Open live-update streams must not block Ctrl+C / restarts.
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()
