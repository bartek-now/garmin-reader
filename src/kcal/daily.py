"""Which days to fetch, and fetching per-day stats; shared by the CLI and MCP server."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from kcal.auth import login
from kcal.client import fetch_activities, fetch_day_summary, fetch_weigh_ins
from kcal.dedupe import build_day_stats
from kcal.models import DayStats


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from None


def _date_range(start: date, end: date, max_days: int | None = None) -> list[date]:
    if end < start:
        raise ValueError(f"End date {end} is before start date {start}.")
    count = (end - start).days + 1
    if max_days is not None and count > max_days:
        raise ValueError(
            f"Range is {count} days; at most {max_days} can be fetched at "
            "once. Split it into smaller ranges."
        )
    return [start + timedelta(days=i) for i in range(count)]


def resolve_days(
    single: str | None,
    start: str | None,
    end: str | None,
    today: date | None = None,
    max_days: int | None = None,
) -> list[date]:
    """Work out which days to fetch.

    - single date alone: that day.
    - start alone: start through yesterday.
    - start and end: that range, inclusive.
    - none given: yesterday only.
    - end without start: an error, since there'd be no start to range from.

    A range longer than `max_days` (if given) is an error.
    """
    today = today or date.today()

    if single and (start or end):
        raise ValueError(
            "Give either a single date or a start/end range, not both."
        )
    if end and not start:
        raise ValueError("An end date needs a start date.")

    if single:
        return [_parse_date(single)]
    if start:
        last = _parse_date(end) if end else today - timedelta(days=1)
        return _date_range(_parse_date(start), last, max_days)
    return [today - timedelta(days=1)]


def fetch_days(days: list[date], login_fn=login) -> list[DayStats]:
    """Stats for consecutive `days`. Daily summaries are one request per day;
    activities and weigh-ins are fetched for the whole range at once.
    """
    if not days:
        return []
    api = login_fn()
    activities = fetch_activities(api, days[0], days[-1])
    weigh_ins = fetch_weigh_ins(api, days[0], days[-1])
    results = []
    for day in days:
        iso = day.isoformat()
        summary = fetch_day_summary(api, day)
        results.append(
            build_day_stats(iso, summary, activities.get(iso, []), weigh_ins.get(iso))
        )
    return results

