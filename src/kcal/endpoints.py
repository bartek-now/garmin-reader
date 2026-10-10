"""Generic, read-only access to the Garmin Connect API for the MCP server.

Every public `get_*` method on `garminconnect.Garmin` is exposed by name. Only
getters are allowed, so nothing that downloads, writes or deletes is reachable.
Over HTTP, only the endpoints in REMOTE_ALLOWED are reachable, and results go
through strip_private().
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Collection
from typing import Any

from garminconnect import Garmin, GarminConnectAuthenticationError

from kcal.auth import GarminLoginError

# Compact per-day summaries; the intraday series (heart_rates, stress_data,
# steps_data, ...) are large and must be requested explicitly.
DEFAULT_DAY_METRICS = [
    "sleep_data",
    "hrv_data",
    "rhr_day",
    "training_readiness",
    "training_status",
    "max_metrics",
    "hydration_data",
    "intensity_minutes_data",
]

# Clients truncate tool results well below this (Claude Desktop somewhere
# between 50k and 68k chars), so stay under the lowest one seen.
MAX_RESULT_CHARS = 50_000
# Lists longer than this are intraday series (per-minute movement, HRV
# readings, ...); the default day summary drops them and keeps scalars.
SUMMARY_MAX_LIST = 20

# Endpoints (without `get_`) reachable over HTTP: health and fitness data.
# Explicit on purpose, so getters added to garminconnect later stay blocked
# remotely until added here. See docs/http-server-design.md, section 5.
REMOTE_ALLOWED = frozenset({
    # Daily metrics
    "stats", "stats_and_body", "user_summary", "daily_steps", "steps_data",
    "floors", "heart_rates", "rhr_day", "hrv_data", "sleep_data",
    "stress_data", "all_day_stress", "body_battery", "body_battery_events",
    "respiration_data", "spo2_data", "hydration_data",
    "intensity_minutes_data", "all_day_events", "weekly_steps",
    "weekly_stress", "weekly_intensity_minutes",
    # Body
    "weigh_ins", "daily_weigh_ins", "body_composition", "blood_pressure",
    # Training
    "training_readiness", "morning_training_readiness", "training_status",
    "max_metrics", "endurance_score", "hill_score", "race_predictions",
    "fitnessage_data", "lactate_threshold", "cycling_ftp",
    "running_tolerance", "personal_record", "progress_summary_between_dates",
    # Activities (completed sessions)
    "activities", "activities_by_date", "activities_fordate", "activity",
    "last_activity", "activity_splits", "activity_split_summaries",
    "activity_typed_splits", "activity_exercise_sets",
    "activity_hr_in_timezones", "activity_power_in_timezones",
    "activity_types",
    # Workouts (planned templates and the schedule)
    "workouts", "workout_by_id", "scheduled_workouts",
    # Nutrition
    "nutrition_daily_food_log", "nutrition_daily_meals",
    "nutrition_daily_settings", "lifestyle_logging_data",
})

# Results sent over HTTP drop every key whose name contains one of these
# words: coordinates, GPS tracks and place names reveal where the owner lives
# and trains; names, profile photos and e-mail identify them. Keys are split
# into words (camelCase or snake_case) and compared whole, so e.g.
# `averageOxygen` doesn't match `geo`.
PRIVATE_KEY_WORDS = frozenset({
    "latitude", "longitude", "polyline", "geo", "location",
    "owner", "fullname", "displayname", "email",
})
# ...or one of these consecutive word pairs.
PRIVATE_KEY_PHRASES = frozenset({
    ("full", "name"), ("display", "name"), ("profile", "image"), ("user", "info"),
})


def _getters() -> dict[str, Any]:
    return {
        name: fn
        for name, fn in inspect.getmembers(Garmin, inspect.isfunction)
        if name.startswith("get_")
    }


def _is_allowed(endpoint: str, allowed: Collection[str] | None) -> bool:
    """`allowed` holds names without `get_`; None means everything."""
    return allowed is None or endpoint.removeprefix("get_") in allowed


def list_endpoints(allowed: Collection[str] | None = None) -> list[dict]:
    out = []
    for name, fn in sorted(_getters().items()):
        if not _is_allowed(name, allowed):
            continue
        doc = (inspect.getdoc(fn) or "").strip().splitlines()
        params = []
        for p in list(inspect.signature(fn).parameters.values())[1:]:
            entry = {"name": p.name, "required": p.default is p.empty}
            if p.default is not p.empty:
                entry["default"] = p.default
            params.append(entry)
        out.append({"endpoint": name, "params": params, "doc": doc[0] if doc else ""})
    return out


def call_endpoint(
    api: Garmin,
    endpoint: str,
    args: dict | None = None,
    allowed: Collection[str] | None = None,
) -> Any:
    """Call `endpoint` on `api`. Everything is checked before `api` is
    touched, so a rejected call makes no Garmin request.
    """
    getters = _getters()
    if not _is_allowed(endpoint, allowed):
        raise ValueError(
            f"Endpoint {endpoint!r} is not available remotely. "
            "Use list_garmin_endpoints to see what is."
        )
    if endpoint not in getters:
        raise ValueError(
            f"Unknown or non-readable endpoint {endpoint!r}. "
            "Use list_garmin_endpoints to see what is available."
        )
    try:
        inspect.signature(getters[endpoint]).bind(api, **(args or {}))
    except TypeError as err:
        raise ValueError(f"Bad arguments for {endpoint}: {err}") from err
    return getattr(api, endpoint)(**(args or {}))


def to_json(value: Any) -> str:
    """Compact JSON, the form results are measured and sent in."""
    return json.dumps(value, default=str, separators=(",", ":"))


def cap_size(result: Any, limit: int = MAX_RESULT_CHARS) -> Any:
    """Return `result`, or a notice if its JSON is too large to hand back."""
    size = len(to_json(result))
    if size <= limit:
        return result
    notice: dict[str, Any] = {
        "error": f"Result is {size:,} chars (limit {limit:,}); only about "
        f"{limit / size:.0%} of it fits. Request less (a shorter date range, "
        "fewer items, or a narrower endpoint) and split the rest into further "
        "calls.",
    }
    if isinstance(result, list):
        notice["items"] = len(result)
    elif isinstance(result, dict):
        notice["top_level_keys"] = list(result)
        if len(to_json(notice)) > limit:
            del notice["top_level_keys"]
    return notice


def summarize(value: Any, max_list: int = SUMMARY_MAX_LIST, hint: str = "") -> Any:
    """Recursively replace long lists (intraday series) with an omission marker,
    followed by `hint` (how to get the full data) if given.
    """
    if isinstance(value, dict):
        return {k: summarize(v, max_list, hint) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > max_list:
            return f"<{len(value)} items omitted{'; ' + hint if hint else ''}>"
        return [summarize(v, max_list, hint) for v in value]
    return value


def _key_words(key: str) -> list[str]:
    """`ownerFullName` -> ["owner", "full", "name"]; `user_info` -> ["user", "info"]."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key).replace("_", " ").lower().split()


def is_private_key(key: Any) -> bool:
    words = _key_words(str(key))
    return bool(PRIVATE_KEY_WORDS.intersection(words)) or any(
        pair in PRIVATE_KEY_PHRASES for pair in zip(words, words[1:])
    )


def strip_private(value: Any) -> Any:
    """Recursively drop keys that are private (see PRIVATE_KEY_WORDS)."""
    if isinstance(value, dict):
        return {k: strip_private(v) for k, v in value.items() if not is_private_key(k)}
    if isinstance(value, list):
        return [strip_private(v) for v in value]
    return value


# garminconnect's docstrings repeat the date format, which the caller already
# knows; e.g. "Return floors data for 'cDate' format 'YYYY-MM-DD'."
_DATE_BOILERPLATE = re.compile(
    r"\s*(?:for |on )?(?:the )?(?:date |day )?'?cdate'?(?: format)? '?YYYY-MM-DD'?", re.I
)


def describe(names: list[str]) -> dict[str, str]:
    """For each name, the first line of garminconnect's docstring for
    get_<name>, without the date-format boilerplate.
    """
    getters = _getters()
    out = {}
    for name in names:
        fn = getters.get(f"get_{name}")
        doc = (inspect.getdoc(fn) or "").strip().splitlines() if fn else []
        out[name] = _DATE_BOILERPLATE.sub("", doc[0]).rstrip(" .") if doc else ""
    return out


def single_day_metrics(allowed: Collection[str] | None = None) -> list[str]:
    """Endpoints whose only parameter is a single date (`cdate`)."""
    return sorted(
        name.removeprefix("get_")
        for name, fn in _getters().items()
        if list(inspect.signature(fn).parameters)[1:] == ["cdate"]
        and _is_allowed(name, allowed)
    )


def day_metrics(
    api: Garmin,
    day: str,
    metrics: list[str] | None = None,
    allowed: Collection[str] | None = None,
) -> dict:
    """Fetch several single-date metrics. Defaults are summarized; explicit ones raw.
    Metrics outside `allowed` get an error entry and no Garmin request.
    """
    wanted = DEFAULT_DAY_METRICS if metrics is None else list(dict.fromkeys(metrics))
    slim = metrics is None
    available = set(single_day_metrics(allowed))
    out: dict[str, Any] = {"date": day}
    for m in wanted:
        if not _is_allowed(m, allowed):
            out[m] = {"error": f"Not available remotely. Available: {sorted(available)}"}
            continue
        if m not in available:
            out[m] = {"error": f"Unknown metric. Available: {sorted(available)}"}
            continue
        try:
            result = getattr(api, f"get_{m}")(day)
            hint = f'pass metrics=["{m}"] for the full list'
            out[m] = summarize(result, hint=hint) if slim else result
        except (GarminLoginError, GarminConnectAuthenticationError):
            raise  # affects every metric: fail the call once, don't retry per metric
        except Exception as err:  # one failing metric shouldn't sink the rest
            out[m] = {"error": f"{type(err).__name__}: {err}"}
    return _fit_total(out)


def _fit_total(out: dict, limit: int = MAX_RESULT_CHARS) -> dict:
    """Replace the largest metrics with notices until the whole response fits."""
    sizes = {k: len(to_json(v)) for k, v in out.items()}
    for k in sorted(sizes, key=sizes.get, reverse=True):
        if len(to_json(out)) <= limit:
            return out
        solo = len(to_json({"date": out.get("date"), k: out[k]}))
        advice = (
            "request this metric on its own"
            if solo <= limit
            else "it is too large to return even on its own"
        )
        out[k] = {
            "error": f"Omitted: {sizes[k]:,} chars would exceed the {limit:,} "
            f"char response limit; {advice}."
        }
    if len(to_json(out)) <= limit:
        return out
    return {"error": f"Response exceeds the {limit:,} char limit; request fewer metrics."}
