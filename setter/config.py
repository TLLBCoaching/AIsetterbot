"""Runtime settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Loads KEY=value lines from .env without overriding variables that are already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip("\"'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Claude
    model: str = os.environ.get("SETTER_MODEL", "claude-opus-5-5")
    effort: str = os.environ.get("SETTER_EFFORT", "medium")

    # GoHighLevel
    ghl_token: str = os.environ.get("GHL_API_TOKEN", "")
    ghl_location_id: str = os.environ.get("GHL_LOCATION_ID", "")
    ghl_base_url: str = os.environ.get("GHL_BASE_URL", "https://services.leadconnectorhq.com")
    webhook_secret: str = os.environ.get("WEBHOOK_SECRET", "")

    # Booking
    booking_link: str = os.environ.get("BOOKING_LINK", "")

    # Behaviour
    bot_enabled: bool = _bool("BOT_ENABLED", True)
    dry_run: bool = _bool("DRY_RUN", False)  # log replies instead of sending them
    debounce_seconds: float = float(os.environ.get("DEBOUNCE_SECONDS", "45"))
    typing_seconds_per_char: float = float(os.environ.get("TYPING_SECONDS_PER_CHAR", "0.04"))
    max_typing_seconds: float = float(os.environ.get("MAX_TYPING_SECONDS", "12"))
    # If a human replied in the conversation within this window, the bot stays quiet.
    human_takeover_minutes: int = int(os.environ.get("HUMAN_TAKEOVER_MINUTES", "720"))

    # Tags written to / read from the GHL contact
    tag_bot_off: str = os.environ.get("TAG_BOT_OFF", "setter-bot-off")
    tag_needs_human: str = os.environ.get("TAG_NEEDS_HUMAN", "setter-needs-human")
    tag_link_sent: str = os.environ.get("TAG_LINK_SENT", "setter-link-sent")
    tag_disqualified: str = os.environ.get("TAG_DISQUALIFIED", "setter-disqualified")

    # Paths
    knowledge_dir: Path = field(default_factory=lambda: ROOT / "knowledge")
    examples_path: Path = field(default_factory=lambda: ROOT / "data" / "examples.jsonl")
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("SETTER_DB", str(ROOT / "data" / "setter.db"))))


settings = Settings()
