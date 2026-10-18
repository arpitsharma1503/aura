"""Application settings, loaded from environment variables and `.env`.

The home location is *not* configured here: it is detected or chosen in the
dashboard and persisted in ``data/location.json`` (see ``app/location.py``).
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def system_timezone() -> str:
    """IANA name of the Pi's own timezone, or UTC if it can't be determined."""
    try:
        parts = Path("/etc/localtime").resolve().parts
        if "zoneinfo" in parts:
            return "/".join(parts[parts.index("zoneinfo") + 1:])
    except OSError:
        pass
    try:
        return Path("/etc/timezone").read_text().strip() or "UTC"
    except OSError:
        return "UTC"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="AURA_",
        extra="ignore",
    )

    # Display. Timezone is used only until a home location (which carries its
    # own timezone) is set; empty = the Pi's system timezone.
    timezone: str = ""
    clock_format: Literal["12h", "24h"] = "12h"

    # Location detection on first launch. "ip" = approximate IP geolocation.
    location_detection: Literal["ip", "off"] = "ip"
    # Optional ipinfo.io token (server-side only, never sent to the browser).
    ipinfo_token: SecretStr | None = None
    location_detect_cooldown_seconds: int = Field(default=60, ge=0)

    # Weather.
    #   open_meteo : live data from Open-Meteo (free, no key, non-commercial)
    #   mock       : clearly labelled development preview with sample data
    #   none       : weather disabled
    weather_provider: Literal["open_meteo", "mock", "none"] = "open_meteo"
    weather_refresh_seconds: int = Field(default=900, ge=60)
    weather_retry_seconds: int = Field(default=120, ge=10)
    weather_max_stale_hours: float = Field(default=6, gt=0)
    http_timeout_seconds: float = Field(default=8, gt=0, le=60)

    # Recommendations look at this many hours ahead ("while you're out").
    travel_window_hours: int = Field(default=6, ge=1, le=24)

    # Who may make changes (groceries, location settings):
    #   lan : any device on the home network (no PIN)
    #   pin : the Pi itself, plus phones that entered household_pin once
    edit_access: Literal["lan", "pin"] = "lan"
    household_pin: SecretStr | None = None
    session_days: int = Field(default=365, ge=1, le=3650)
    trust_localhost: bool = True
    # URL phones should use (shown as a QR code). Empty = the Pi's current
    # LAN IP and port, detected on every request.
    public_url: str = ""

    # Network. Bind to IPv4 only by default; see README "Security".
    host: str = "0.0.0.0"
    port: int = 8080
    # Extra hostnames accepted in the Host header, in addition to private IPs.
    allowed_hosts: Annotated[list[str], NoDecode] = ["localhost", "aura-pi", "aura-pi.local"]
    # Reject clients whose address is not loopback / private / link-local.
    lan_only: bool = True

    # Storage
    data_dir: Path = PROJECT_ROOT / "data"
    log_dir: Path = PROJECT_ROOT / "logs"

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [h.strip().lower() for h in value.split(",") if h.strip()]
        return value

    @field_validator("household_pin", mode="before")
    @classmethod
    def _pin(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, str) and len(value.strip()) < 6:
            raise ValueError("AURA_HOUSEHOLD_PIN must be at least 6 characters")
        return value.strip() if isinstance(value, str) else value

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("AURA_PUBLIC_URL must start with http:// or https://")
        return value

    @property
    def default_timezone(self) -> str:
        return self.timezone or system_timezone()


@lru_cache
def get_settings() -> Settings:
    return Settings()
