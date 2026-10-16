"""Google Calendar through "Sign in with Google" (OAuth): read events, add them, move them.

You create an OAuth client in Google Cloud (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET), then press
"Connect Google Calendar" in HQ once. HQ keeps the refresh token in its database so the connection
works on every device.
"""

import logging
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode

import httpx

from .config import hq_settings
from .store import HQStore

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
# Read and change events on your calendars. Nothing else: no email, no contacts, no calendar settings.
SCOPE = "https://www.googleapis.com/auth/calendar.events"
TOKEN_KEY = "google_refresh_token"
ACCOUNT_KEY = "google_account"


class NeedsReconnect(Exception):
    """Google rejected the saved connection (revoked or expired); press Connect again."""


class GoogleCalendar:
    def __init__(self, store: HQStore, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self._transport = transport
        self._access_token = ""
        self._expires_at = 0.0

    # Setup state

    @property
    def configured(self) -> bool:
        return bool(hq_settings.google_client_id and hq_settings.google_client_secret)

    @property
    def connected(self) -> bool:
        return self.configured and bool(self.store.get_setting(TOKEN_KEY))

    @property
    def account(self) -> str:
        return self.store.get_setting(ACCOUNT_KEY) or ""

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20.0, transport=self._transport)

    # OAuth

    def auth_url(self, state: str, redirect_uri: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": hq_settings.google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",  # ask for a refresh token so HQ stays connected
            "prompt": "consent select_account",  # always ask which Google account to use
            "include_granted_scopes": "true",
            "state": state,
        })

    async def finish_connect(self, code: str, redirect_uri: str) -> None:
        async with self._http() as http:
            resp = await http.post(TOKEN_URL, data={
                "code": code,
                "client_id": hq_settings.google_client_id,
                "client_secret": hq_settings.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            })
        resp.raise_for_status()
        tokens = resp.json()
        if not tokens.get("refresh_token"):
            raise RuntimeError("Google didn't return a refresh token. Remove HQ's access in your Google account and try again.")
        self.store.set_setting(TOKEN_KEY, tokens["refresh_token"])
        self._access_token = tokens.get("access_token", "")
        self._expires_at = time.monotonic() + int(tokens.get("expires_in", 0)) - 60
        try:
            primary = await self._request("GET", "/calendars/primary")
            self.store.set_setting(ACCOUNT_KEY, primary.get("summary") or primary.get("id") or "")
        except Exception:
            log.exception("Connected, but couldn't read the primary calendar's name")

    async def disconnect(self) -> None:
        token = self.store.get_setting(TOKEN_KEY)
        self.store.delete_setting(TOKEN_KEY)
        self.store.delete_setting(ACCOUNT_KEY)
        self._access_token, self._expires_at = "", 0.0
        if token:
            try:
                async with self._http() as http:
                    await http.post(REVOKE_URL, data={"token": token})
            except httpx.HTTPError:
                log.warning("Couldn't revoke the Google token; it's removed from HQ anyway")

    async def _token(self) -> str:
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token
        refresh = self.store.get_setting(TOKEN_KEY)
        if not refresh:
            raise NeedsReconnect()
        async with self._http() as http:
            resp = await http.post(TOKEN_URL, data={
                "client_id": hq_settings.google_client_id,
                "client_secret": hq_settings.google_client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            })
        if resp.status_code in (400, 401):
            # invalid_grant: access was removed in Google, or the token expired.
            log.warning("Google refused the saved connection: %s", resp.text[:200])
            raise NeedsReconnect()
        resp.raise_for_status()
        tokens = resp.json()
        self._access_token = tokens["access_token"]
        self._expires_at = time.monotonic() + int(tokens.get("expires_in", 3600)) - 60
        return self._access_token

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        token = await self._token()
        async with self._http() as http:
            resp = await http.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        if resp.status_code == 401:
            self._access_token = ""
            raise NeedsReconnect()
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # Events

    async def list_events(self, start: datetime, end: datetime) -> list[dict]:
        tz = hq_settings.tz
        events = []
        for cal_id in hq_settings.google_calendar_ids:
            page_token = None
            for _ in range(5):
                params = {
                    "timeMin": start.isoformat(), "timeMax": end.isoformat(),
                    "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
                    "timeZone": hq_settings.timezone,
                }
                if page_token:
                    params["pageToken"] = page_token
                data = await self._request("GET", f"/calendars/{quote(cal_id, safe='')}/events", params=params)
                for item in data.get("items", []):
                    ev = to_event(item, tz, cal_id)
                    if ev:
                        events.append(ev)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        return events

    async def create_event(self, title: str, day: str, start_time: str = "", duration_minutes: int = 60,
                           location: str = "", notes: str = "") -> dict:
        body = {"summary": title, "location": location, "description": notes,
                **event_times(day, start_time, duration_minutes)}
        item = await self._request("POST", "/calendars/primary/events", json=body)
        return to_event(item, hq_settings.tz, "primary")

    async def update_event(self, event_id: str, calendar_id: str = "primary", title: str | None = None,
                           day: str | None = None, start_time: str | None = None,
                           duration_minutes: int | None = None, location: str | None = None) -> dict:
        path = f"/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}"
        body: dict = {}
        if title is not None:
            body["summary"] = title
        if location is not None:
            body["location"] = location
        if day is not None or start_time is not None or duration_minutes is not None:
            current = to_event(await self._request("GET", path), hq_settings.tz, calendar_id)
            new_day = day or current["start"][:10]
            new_time = current["start"][11:16] if start_time is None and not current["all_day"] else (start_time or "")
            if duration_minutes is None:
                length = datetime.fromisoformat(current["end"]) - datetime.fromisoformat(current["start"])
                duration_minutes = max(15, int(length.total_seconds() // 60)) if not current["all_day"] else 60
            body.update(event_times(new_day, new_time, duration_minutes))
        item = await self._request("PATCH", path, json=body)
        return to_event(item, hq_settings.tz, calendar_id)

    async def delete_event(self, event_id: str, calendar_id: str = "primary") -> None:
        await self._request("DELETE", f"/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}")


def event_times(day: str, start_time: str, duration_minutes: int) -> dict:
    """Google's start/end for an event: all-day when there's no start time."""
    first = date.fromisoformat(day)
    if not start_time:
        return {"start": {"date": first.isoformat()}, "end": {"date": (first + timedelta(days=1)).isoformat()}}
    begins = datetime.fromisoformat(f"{first.isoformat()}T{start_time}")
    ends = begins + timedelta(minutes=max(5, int(duration_minutes)))
    tz = hq_settings.timezone
    return {"start": {"dateTime": begins.isoformat(timespec="minutes") + ":00", "timeZone": tz},
            "end": {"dateTime": ends.isoformat(timespec="minutes") + ":00", "timeZone": tz}}


def to_event(item: dict, tz, calendar_id: str) -> dict | None:
    if item.get("status") == "cancelled":
        return None
    start, end = item.get("start") or {}, item.get("end") or {}
    if "dateTime" in start:
        begins = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(tz)
        ends = datetime.fromisoformat((end.get("dateTime") or start["dateTime"]).replace("Z", "+00:00")).astimezone(tz)
        all_day = False
    elif "date" in start:
        d = date.fromisoformat(start["date"])
        e = date.fromisoformat(end.get("date") or (d + timedelta(days=1)).isoformat())
        begins = datetime(d.year, d.month, d.day, tzinfo=tz)
        ends = datetime(e.year, e.month, e.day, tzinfo=tz)
        all_day = True
    else:
        return None
    return {
        "id": item.get("id", ""),
        "calendar": calendar_id,
        "title": item.get("summary") or "(no title)",
        "start": begins.isoformat(),
        "end": ends.isoformat(),
        "all_day": all_day,
        "location": item.get("location") or "",
        "description": (item.get("description") or "")[:500],
        "url": item.get("htmlLink") or "",
        "editable": True,
    }
