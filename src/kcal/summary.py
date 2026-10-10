"""get_garmin_summary: one compact row per day across a date range.

Each metric group is fetched as cheaply as Garmin allows: some with one
request for the whole range (or per 28-day chunk), some with one request per
day. The per-day groups share one request per day (Garmin's daily summary),
except `lifestyle`, which has its own.
"""

from __future__ import annotations

import contextvars
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from garminconnect import GarminConnectAuthenticationError

from kcal.auth import GarminLoginError
from kcal.client import fetch_activities, fetch_day_summary, fetch_weigh_ins
from kcal.dedupe import build_day_stats, build_weight_row
from kcal.models import Workout

Group = Literal[
    "activity", "heart", "stress", "lifestyle", "weight", "workouts", "hrv", "sleep", "readiness"
]

# Groups needing one or more Garmin requests per day.
PER_DAY_GROUPS = frozenset({"activity", "heart", "stress", "lifestyle"})
DEFAULT_GROUPS: tuple[Group, ...] = ("activity", "weight", "sleep", "hrv")

# Range limits: per-day groups are one request per day, made one after
# another; the others cost one request per range (or per 28-day chunk).
MAX_PER_DAY_DAYS = 120
MAX_RANGE_DAYS = 366
SLEEP_CHUNK_DAYS = 28  # Garmin's limit for the sleep statistics endpoint
# Per-day requests run this many at a time.
PER_DAY_WORKERS = 4

# The range endpoints below aren't garminconnect getters; they're reached with
# connectapi(path). Over HTTP only paths matching this are allowed.
RANGE_PATHS = re.compile(
    r"/(hrv-service/hrv/daily|sleep-service/stats/sleep/daily"
    r"|metrics-service/metrics/trainingreadiness)/\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}"
)

# Each group's columns, in output order.
GROUP_FIELDS: dict[str, tuple[str, ...]] = {
    "activity": (
        "steps", "non_workout_steps", "active_calories", "passive_calories",
        "workout_calories", "workout_active_calories", "intensity_minutes_moderate",
        "intensity_minutes_vigorous", "floors_climbed",
    ),
    "heart": ("resting_hr", "resting_hr_7d_avg", "min_hr", "max_hr"),
    "stress": (
        "stress_avg", "stress_max", "body_battery_high", "body_battery_low",
        "body_battery_charged", "body_battery_drained", "body_battery_at_wake",
    ),
    "lifestyle": ("logged_yes", "logged_no"),
    "weight": ("weight_kg", "body_fat_pct", "muscle_mass_kg", "weigh_ins"),
    "workouts": ("workouts",),
    "hrv": (
        "hrv_last_night_ms", "hrv_last_night_5min_high_ms", "hrv_weekly_avg_ms",
        "hrv_baseline_low_ms", "hrv_baseline_high_ms", "hrv_status",
    ),
    "sleep": (
        "sleep_score", "sleep_quality", "sleep_start", "sleep_end", "sleep_minutes",
        "deep_sleep_minutes", "light_sleep_minutes", "rem_sleep_minutes", "awake_minutes",
        "sleep_avg_hr", "sleep_respiration", "sleep_spo2", "sleep_body_battery_change",
        "sleep_need_minutes",
    ),
    "readiness": (
        "readiness_score", "readiness_level", "acute_training_load", "recovery_time_minutes",
    ),
}

GROUP_DOCS = {
    "activity": "steps, non_workout_steps (workout steps removed), active_calories, "
    "passive_calories (resting/BMR), workout_calories (gross), workout_active_calories "
    "(net of BMR), intensity_minutes_moderate/_vigorous, floors_climbed. 1 request/day.",
    "heart": "resting_hr, resting_hr_7d_avg, min_hr, max_hr (bpm). Shares the "
    "activity request.",
    "stress": "stress_avg, stress_max (0-100), body_battery_high/_low/_charged/"
    "_drained/_at_wake. Shares the activity request.",
    "lifestyle": "logged_yes / logged_no: behaviours logged in Garmin's lifestyle "
    "log that day (e.g. Late Meals, Heavy Meals, Moderate Exercise); unlogged ones "
    "are omitted. 1 request/day.",
    "weight": "weight_kg, body_fat_pct, muscle_mass_kg from the day's last "
    "weigh-in; weigh_ins when there were several. 1 request/range.",
    "workouts": "workouts: list of {id, name, type, start (HH:MM), minutes, "
    "calories (gross), active_calories (net of BMR), steps}; id works with "
    "get_activity. 1 request/range.",
    "hrv": "hrv_last_night_ms (overnight average), hrv_last_night_5min_high_ms, "
    "hrv_weekly_avg_ms, hrv_baseline_low_ms/_high_ms (Garmin's balanced range), "
    "hrv_status. 1 request/range.",
    "sleep": "sleep_score, sleep_quality, sleep_start/sleep_end (local HH:MM), "
    "sleep_minutes, deep/light/rem_sleep_minutes, awake_minutes, sleep_avg_hr, "
    "sleep_respiration, sleep_spo2, sleep_body_battery_change, sleep_need_minutes. "
    "1 request per 28 days.",
    "readiness": "readiness_score (0-100), readiness_level, acute_training_load, "
    "recovery_time_minutes, from the morning (after wake-up) assessment. "
    "1 request/range.",
}


def check_range(days: list[date], groups: list[str]) -> None:
    per_day = sorted(PER_DAY_GROUPS.intersection(groups))
    limit = MAX_PER_DAY_DAYS if per_day else MAX_RANGE_DAYS
    if len(days) > limit:
        why = (
            f"with {', '.join(per_day)} (one Garmin request per day) at most "
            f"{MAX_PER_DAY_DAYS} days; without per-day groups up to {MAX_RANGE_DAYS}"
            if per_day
            else f"at most {MAX_RANGE_DAYS} days"
        )
        raise ValueError(f"Range is {len(days)} days; {why}. Split it into smaller ranges.")


def summarize(api: Any, days: list[date], groups: list[str]) -> dict:
    """A table: {"fields": ["date", ...], "days": [[...], ...]}, one array of
    values per day (oldest first) in the order of `fields`, null where Garmin
    reported nothing. Field names appear once rather than on every day, which
    keeps results compact. Fields with no value on any day, and days with no
    values, are left out.

    A group whose data can't be fetched doesn't sink the others: the result
    then also has {"errors": {group: message}}. Garmin login problems still
    fail the whole call, since every group would hit them.
    """
    groups = list(dict.fromkeys(groups))
    check_range(days, groups)
    if not days:
        return {"fields": ["date"], "days": []}
    start, end = days[0], days[-1]
    rows: dict[str, dict] = {d.isoformat(): {} for d in days}
    errors: dict[str, str] = {}

    def add(fields_by_day: dict[str, dict]) -> None:
        for iso, fields in fields_by_day.items():
            if iso in rows:
                rows[iso].update(fields)

    def fetch(failing_groups: list[str], fn):
        """fn(), or None with an error recorded for each of the groups."""
        try:
            return fn()
        except _LOGIN_ERRORS:
            raise
        except Exception as err:
            for g in failing_groups:
                errors[g] = _message(err)
            return None

    activities: dict = {}
    if {"activity", "workouts"} & set(groups):
        # activity needs them to take workout steps out of the daily total.
        needing = [g for g in ("activity", "workouts") if g in groups]
        activities = fetch(needing, lambda: fetch_activities(api, start, end))
        if activities is None:
            groups = [g for g in groups if g not in needing]
            activities = {}
    if "workouts" in groups:
        add({iso: {"workouts": [_workout(a) for a in acts]} for iso, acts in activities.items()})
    for group, fn in (
        ("weight", lambda: _weights(fetch_weigh_ins(api, start, end))),
        ("hrv", lambda: _hrv(api, start, end)),
        ("sleep", lambda: _sleep(api, start, end)),
        ("readiness", lambda: _readiness(api, start, end)),
    ):
        if group in groups:
            add(fetch([group], fn) or {})

    daily_groups = [g for g in ("activity", "heart", "stress") if g in groups]
    lifestyle = "lifestyle" in groups

    def per_day(day: date) -> tuple[dict, dict]:
        iso, fields, failed = day.isoformat(), {}, {}
        if daily_groups:
            try:
                fields.update(
                    _daily(fetch_day_summary(api, day), activities.get(iso, []), daily_groups)
                )
            except _LOGIN_ERRORS:
                raise
            except Exception as err:
                failed.update({g: _message(err) for g in daily_groups})
        if lifestyle:
            try:
                fields.update(_lifestyle(api.get_lifestyle_logging_data(iso)))
            except _LOGIN_ERRORS:
                raise
            except Exception as err:
                failed["lifestyle"] = _message(err)
        return fields, failed

    if daily_groups or lifestyle:
        failures: dict[str, list[tuple[str, str]]] = {}
        for day, (fields, failed) in zip(days, _map_per_day(per_day, days)):
            add({day.isoformat(): fields})
            for g, message in failed.items():
                failures.setdefault(g, []).append((day.isoformat(), message))
        for g, failed_days in failures.items():
            first_day, message = failed_days[0]
            errors[g] = f"{len(failed_days)} day(s) failed, e.g. {first_day}: {message}"

    out = _table(rows, list(dict.fromkeys(groups + list(errors))))
    if errors:
        out["errors"] = errors
    return out


# Problems with the Garmin session affect every group, so they fail the call
# (and the MCP server turns them into "sign in again" instructions).
_LOGIN_ERRORS = (GarminLoginError, GarminConnectAuthenticationError)


def _message(err: Exception) -> str:
    return f"{type(err).__name__}: {err}"[:300]


def _map_per_day(fn, days: list[date]) -> list[dict]:
    """fn over the days, a few at a time. The first day runs alone, so a
    Garmin token that needs refreshing is refreshed once before the rest run
    in parallel. Each worker runs in a copy of the caller's context (the MCP
    server keeps the exposure policy there).
    """
    first = [fn(days[0])]
    if len(days) == 1:
        return first
    ctx = contextvars.copy_context()
    # If a day fails the call, map's results are abandoned and it cancels the
    # days still queued, so they aren't sent to Garmin.
    with ThreadPoolExecutor(PER_DAY_WORKERS) as pool:
        return first + list(pool.map(lambda d: ctx.copy().run(fn, d), days[1:]))


def _table(rows: dict[str, dict], groups: list[str]) -> dict:
    present = {
        iso: {k: v for k, v in fields.items() if v is not None and v != []}
        for iso, fields in rows.items()
    }
    seen = {k for values in present.values() for k in values}
    fields = [f for g in groups for f in GROUP_FIELDS[g] if f in seen]
    return {
        "fields": ["date", *fields],
        "days": [[iso, *(values.get(f) for f in fields)] for iso, values in present.items() if values],
    }


# --- groups ---------------------------------------------------------------------


def _workout(raw: dict) -> dict:
    w = Workout.from_raw(raw)
    out = dict(
        id=w.activity_id,
        name=w.name,
        type=w.activity_type,
        start=(w.start_time or "")[11:16] or None,
        minutes=round(w.duration_seconds / 60),
        calories=round(w.calories),
        active_calories=round(w.active_calories),
        steps=w.steps or None,
    )
    return {k: v for k, v in out.items() if v is not None}


def _weights(weigh_ins: dict[str, dict]) -> dict[str, dict]:
    out = {}
    for iso, weigh_in in weigh_ins.items():
        row = build_weight_row(iso, weigh_in)
        if row is not None:
            row.pop("date")
            if "count" in row:
                row["weigh_ins"] = row.pop("count")
            out[iso] = row
    return out


def _daily(summary: dict, activities: list[dict], groups: list[str]) -> dict:
    s = summary
    out: dict[str, Any] = {}
    # No step total means Garmin has nothing for the day (watch not worn,
    # not owned yet, in the future): no activity numbers rather than zeros.
    if "activity" in groups and s.get("totalSteps") is not None:
        stats = build_day_stats("", s, activities)
        out.update(
            steps=stats.total_steps,
            non_workout_steps=stats.non_workout_steps,
            active_calories=round(stats.active_calories),
            passive_calories=round(stats.bmr_calories),
            workout_calories=round(stats.workout_calories),
            workout_active_calories=round(stats.workout_active_calories),
            intensity_minutes_moderate=s.get("moderateIntensityMinutes"),
            intensity_minutes_vigorous=s.get("vigorousIntensityMinutes"),
            floors_climbed=_round(s.get("floorsAscended")),
        )
    if "heart" in groups:
        out.update(
            resting_hr=s.get("restingHeartRate"),
            resting_hr_7d_avg=s.get("lastSevenDaysAvgRestingHeartRate"),
            min_hr=s.get("minHeartRate"),
            max_hr=s.get("maxHeartRate"),
        )
    if "stress" in groups:
        out.update(
            stress_avg=_positive(s.get("averageStressLevel")),
            stress_max=_positive(s.get("maxStressLevel")),
            body_battery_high=s.get("bodyBatteryHighestValue"),
            body_battery_low=s.get("bodyBatteryLowestValue"),
            body_battery_charged=s.get("bodyBatteryChargedValue"),
            body_battery_drained=s.get("bodyBatteryDrainedValue"),
            body_battery_at_wake=s.get("bodyBatteryAtWakeTime"),
        )
    return out


def _lifestyle(log: dict) -> dict:
    items = (log or {}).get("dailyLogsReport") or []
    by_status: dict[str, list[str]] = {"YES": [], "NO": []}
    for item in items:
        status = item.get("logStatus")
        if status in by_status and item.get("name"):
            by_status[status].append(item["name"])
    return {"logged_yes": by_status["YES"], "logged_no": by_status["NO"]}


def _hrv(api: Any, start: date, end: date) -> dict[str, dict]:
    resp = api.connectapi(f"/hrv-service/hrv/daily/{start}/{end}") or {}
    out = {}
    for h in resp.get("hrvSummaries") or []:
        baseline = h.get("baseline") or {}
        out[h["calendarDate"]] = dict(
            hrv_last_night_ms=h.get("lastNightAvg"),
            hrv_last_night_5min_high_ms=h.get("lastNight5MinHigh"),
            hrv_weekly_avg_ms=h.get("weeklyAvg"),
            hrv_baseline_low_ms=baseline.get("balancedLow"),
            hrv_baseline_high_ms=baseline.get("balancedUpper"),
            hrv_status=h.get("status"),
        )
    return out


def _sleep(api: Any, start: date, end: date) -> dict[str, dict]:
    out = {}
    for chunk_start, chunk_end in _chunks(start, end, SLEEP_CHUNK_DAYS):
        resp = api.connectapi(f"/sleep-service/stats/sleep/daily/{chunk_start}/{chunk_end}") or {}
        for night in resp.get("individualStats") or []:
            v = night.get("values") or {}
            out[night["calendarDate"]] = dict(
                sleep_score=v.get("sleepScore"),
                sleep_quality=v.get("sleepScoreQuality"),
                sleep_start=_local_time(v.get("localSleepStartTimeInMillis")),
                sleep_end=_local_time(v.get("localSleepEndTimeInMillis")),
                sleep_minutes=_minutes(v.get("totalSleepTimeInSeconds")),
                deep_sleep_minutes=_minutes(v.get("deepTime")),
                light_sleep_minutes=_minutes(v.get("lightTime")),
                rem_sleep_minutes=_minutes(v.get("remTime")),
                awake_minutes=_minutes(v.get("awakeTime")),
                sleep_avg_hr=_round(v.get("avgHeartRate")),
                sleep_respiration=_round(v.get("respiration"), 1),
                sleep_spo2=_round(v.get("spO2"), 1),
                sleep_body_battery_change=v.get("bodyBatteryChange"),
                sleep_need_minutes=v.get("sleepNeed"),
            )
    return out


def _readiness(api: Any, start: date, end: date) -> dict[str, dict]:
    entries = api.connectapi(f"/metrics-service/metrics/trainingreadiness/{start}/{end}") or []
    chosen: dict[str, tuple[tuple, dict]] = {}
    for e in entries:
        day = e.get("calendarDate")
        if day is None:
            continue
        # The morning assessment if there is one, else the day's earliest.
        key = (e.get("inputContext") != "AFTER_WAKEUP_RESET", e.get("timestamp") or "")
        if day not in chosen or key < chosen[day][0]:
            chosen[day] = (key, e)
    return {
        day: dict(
            readiness_score=e.get("score"),
            readiness_level=e.get("level"),
            acute_training_load=e.get("acuteLoad"),
            recovery_time_minutes=e.get("recoveryTime"),
        )
        for day, (_, e) in chosen.items()
    }


# --- helpers --------------------------------------------------------------------


def _chunks(start: date, end: date, size: int):
    while start <= end:
        chunk_end = min(start + timedelta(days=size - 1), end)
        yield start, chunk_end
        start = chunk_end + timedelta(days=1)


def _local_time(millis: int | None) -> str | None:
    """HH:MM. Garmin's "local" millis are wall-clock time encoded as if UTC."""
    if millis is None:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).strftime("%H:%M")


def _minutes(seconds: int | None) -> int | None:
    return None if seconds is None else round(seconds / 60)


def _round(value: float | None, digits: int | None = None) -> float | int | None:
    return None if value is None else round(value, digits)


def _positive(value: int | None) -> int | None:
    """Garmin reports -1/-2 when there wasn't enough data."""
    return value if value is not None and value >= 0 else None


def describe_groups() -> str:
    return "\n".join(f"- {name}: {doc}" for name, doc in GROUP_DOCS.items())

