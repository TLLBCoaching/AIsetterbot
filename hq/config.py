"""Settings for HQ, the personal dashboard. Read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

# Importing the setter config loads .env, and HQ reuses its Claude and GoHighLevel settings.
from setter.config import ROOT, _bool
from setter.config import settings as setter_settings


def _list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


@dataclass(frozen=True)
class HQSettings:
    # Login. HQ refuses to serve anything until a password and secret key are set.
    password: str = os.environ.get("HQ_PASSWORD", "")
    secret_key: str = os.environ.get("HQ_SECRET_KEY", "")
    session_days: int = int(os.environ.get("HQ_SESSION_DAYS", "30"))
    # Set to false only when running locally over plain http.
    secure_cookies: bool = _bool("HQ_SECURE_COOKIES", True)

    timezone: str = os.environ.get("HQ_TIMEZONE", "Australia/Sydney")
    currency: str = os.environ.get("HQ_CURRENCY", "AUD")

    # Claude
    model: str = os.environ.get("HQ_MODEL", setter_settings.model)
    effort: str = os.environ.get("HQ_EFFORT", "medium")

    # Calendar: one or more private iCal (.ics) URLs, comma separated.
    # Google Calendar → Settings → your calendar → "Secret address in iCal format".
    calendar_urls: list[str] = field(default_factory=lambda: _list("HQ_CALENDAR_ICS_URLS"))

    # Finances: a Stripe restricted key with read access to balance, charges, refunds and subscriptions.
    stripe_key: str = os.environ.get("STRIPE_API_KEY", "")

    db_path: Path = field(default_factory=lambda: Path(os.environ.get("HQ_DB", str(ROOT / "data" / "hq.db"))))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def ghl_configured(self) -> bool:
        return bool(setter_settings.ghl_token and setter_settings.ghl_location_id)


hq_settings = HQSettings()
