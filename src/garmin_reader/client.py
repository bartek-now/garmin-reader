"""Thin wrapper around the Garmin Connect API calls garmin-reader needs."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from garminconnect import Garmin


def fetch_day_summary(api: Garmin, day: date) -> dict:
    return api.get_user_summary(day.isoformat())


def fetch_activities(api: Garmin, start: date, end: date) -> dict[str, list[dict]]:
    """Activities in [start, end], grouped by local start date (YYYY-MM-DD)."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for a in api.get_activities_by_date(start.isoformat(), end.isoformat()):
        by_day[(a.get("startTimeLocal") or "")[:10]].append(a)
    return by_day


def fetch_weigh_ins(api: Garmin, start: date, end: date) -> dict[str, dict]:
    """Weigh-ins in [start, end] by date, each shaped like a single-day
    response (`{"dateWeightList": [...]}`).
    """
    resp = api.get_weigh_ins(start.isoformat(), end.isoformat()) or {}
    return {
        s["summaryDate"]: {"dateWeightList": s.get("allWeightMetrics") or []}
        for s in resp.get("dailyWeightSummaries") or []
    }
