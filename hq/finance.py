"""Finances: business money from Stripe (read-only) plus a manual money log for everything else."""

import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx

from .config import hq_settings
from .store import HQStore

log = logging.getLogger(__name__)

STRIPE_API = "https://api.stripe.com/v1"
CACHE_SECONDS = 600
_cache: dict[str, tuple[float, dict]] = {}


class StripeClient:
    def __init__(self, key: str):
        self._http = httpx.AsyncClient(base_url=STRIPE_API, auth=(key, ""), timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, **params) -> dict:
        resp = await self._http.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def list_all(self, path: str, max_items: int = 1000, **params) -> list[dict]:
        items, starting_after = [], None
        while len(items) < max_items:
            page_params = dict(params, limit=100)
            if starting_after:
                page_params["starting_after"] = starting_after
            page = await self.get(path, **page_params)
            data = page.get("data", [])
            items.extend(data)
            if not page.get("has_more") or not data:
                break
            starting_after = data[-1]["id"]
        return items


def _cents(amount: int | None) -> float:
    return round((amount or 0) / 100, 2)


def summarise_charges(charges: list[dict], today: date, tz, currency: str) -> dict:
    """Cash collected (net of refunds) this week, this month, last month and per week, plus failed payments."""
    currency = currency.lower()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    last_month_start = (month_start - timedelta(days=1)).replace(day=1)

    totals = {"this_week": 0.0, "this_month": 0.0, "last_month": 0.0}
    weekly: dict[str, float] = defaultdict(float)
    failed = []
    for ch in charges:
        if ch.get("currency", currency) != currency:
            continue
        day = datetime.fromtimestamp(ch["created"], tz).date()
        if ch.get("status") == "failed":
            billing = ch.get("billing_details") or {}
            failed.append({
                "date": day.isoformat(),
                "amount": _cents(ch.get("amount")),
                "customer": billing.get("name") or billing.get("email") or ch.get("customer") or "",
                "reason": (ch.get("outcome") or {}).get("seller_message") or ch.get("failure_message") or "",
            })
            continue
        if ch.get("status") != "succeeded" or not ch.get("paid", True):
            continue
        net = _cents(ch.get("amount_captured", ch.get("amount")) - (ch.get("amount_refunded") or 0))
        weekly[(day - timedelta(days=day.weekday())).isoformat()] += net
        if day >= week_start:
            totals["this_week"] += net
        if day >= month_start:
            totals["this_month"] += net
        elif day >= last_month_start:
            totals["last_month"] += net

    weeks = [{"week_of": k, "amount": round(v, 2)} for k, v in sorted(weekly.items())]
    return {
        **{k: round(v, 2) for k, v in totals.items()},
        "weekly": weeks,
        "failed": sorted(failed, key=lambda f: f["date"], reverse=True)[:20],
    }


async def stripe_summary(today: date) -> dict:
    if not hq_settings.stripe_key:
        return {"configured": False}
    cached = _cache.get("stripe")
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]

    tz = hq_settings.tz
    # Enough history to cover last month and ~12 weeks of trend.
    since = datetime.combine(today - timedelta(days=90), datetime.min.time(), tz)
    client = StripeClient(hq_settings.stripe_key)
    try:
        balance = await client.get("/balance")
        charges = await client.list_all("/charges", **{"created[gte]": int(since.timestamp())})
        subs = await client.list_all("/subscriptions", status="active")
    except httpx.HTTPError as exc:
        log.error("Stripe request failed: %s", type(exc).__name__)
        return {"configured": True, "error": "Couldn't reach Stripe. Check STRIPE_API_KEY and its permissions."}
    finally:
        await client.aclose()

    currency = hq_settings.currency.lower()

    def pick(entries: list[dict]) -> float:
        return sum(_cents(e.get("amount")) for e in entries if e.get("currency") == currency)

    summary = {
        "configured": True,
        "currency": hq_settings.currency,
        "balance_available": pick(balance.get("available", [])),
        "balance_pending": pick(balance.get("pending", [])),
        "active_subscriptions": len(subs),
        **summarise_charges(charges, today, tz, currency),
        "as_of": datetime.now(tz).isoformat(timespec="minutes"),
    }
    _cache["stripe"] = (time.monotonic(), summary)
    return summary


def money_log_summary(store: HQStore, today: date) -> dict:
    """Totals from the manual log for this month, split by category."""
    month_start = today.replace(day=1).isoformat()
    entries = store.list_money(since=month_start)
    by_category: dict[str, float] = defaultdict(float)
    money_in = money_out = 0.0
    for e in entries:
        by_category[e["category"]] += e["amount"]
        if e["amount"] >= 0:
            money_in += e["amount"]
        else:
            money_out += e["amount"]
    return {
        "month_in": round(money_in, 2),
        "month_out": round(money_out, 2),
        "month_net": round(money_in + money_out, 2),
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda kv: kv[1])},
        "recent": store.list_money()[:30],
    }
