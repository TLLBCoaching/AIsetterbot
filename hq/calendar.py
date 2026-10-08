"""Read-only calendar from one or more private iCal (.ics) feeds, with recurring events expanded."""

import logging
import time
from datetime import date, datetime, timedelta

import httpx
import icalendar
import recurring_ical_events

from .config import hq_settings

log = logging.getLogger(__name__)

CACHE_SECONDS = 300
_cache: dict[str, tuple[float, icalendar.Calendar]] = {}


async def _fetch(url: str) -> icalendar.Calendar | None:
    cached = _cache.get(url)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
            resp = await http.get(url)
            resp.raise_for_status()
        cal = icalendar.Calendar.from_ical(resp.content)
    except Exception:
        # Never log the URL: a private iCal address is a credential.
        log.exception("Couldn't load a calendar feed")
        return cached[1] if cached else None
    _cache[url] = (time.monotonic(), cal)
    return cal


def _as_local(value: date | datetime, tz) -> tuple[datetime, bool]:
    """Returns (local datetime, all_day)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(tz), False
    return datetime(value.year, value.month, value.day, tzinfo=tz), True


def events_from_calendar(cal: icalendar.Calendar, start: datetime, end: datetime, tz, source: int = 0) -> list[dict]:
    events = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        begins, all_day = _as_local(ev.decoded("DTSTART"), tz)
        if ev.get("DTEND") is not None:
            ends, _ = _as_local(ev.decoded("DTEND"), tz)
        elif ev.get("DURATION") is not None:
            ends = begins + ev.decoded("DURATION")
        else:
            ends = begins + (timedelta(days=1) if all_day else timedelta(0))
        events.append({
            "title": str(ev.get("SUMMARY", "(no title)")),
            "start": begins.isoformat(),
            "end": ends.isoformat(),
            "all_day": all_day,
            "location": str(ev.get("LOCATION", "") or ""),
            "description": str(ev.get("DESCRIPTION", "") or "")[:500],
            "source": source,
        })
    return events


async def get_events(start: datetime, end: datetime) -> dict:
    """Events between start and end (timezone-aware), sorted. Feeds that fail are reported, not fatal."""
    tz = hq_settings.tz
    if not hq_settings.calendar_urls:
        return {"configured": False, "events": [], "errors": 0}
    events, errors = [], 0
    for i, url in enumerate(hq_settings.calendar_urls):
        cal = await _fetch(url)
        if cal is None:
            errors += 1
            continue
        try:
            events.extend(events_from_calendar(cal, start, end, tz, source=i))
        except Exception:
            log.exception("Couldn't read events from calendar feed %d", i)
            errors += 1
    events.sort(key=lambda e: (e["start"], not e["all_day"]))
    return {"configured": True, "events": events, "errors": errors}


def day_bounds(day: date, days: int = 1) -> tuple[datetime, datetime]:
    tz = hq_settings.tz
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start, start + timedelta(days=days)
