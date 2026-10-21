"""Generic, read-only access to the Garmin Connect API for the MCP server.

Every public `get_*` method on `garminconnect.Garmin` is exposed by name. Only
getters are allowed, so nothing that downloads, writes or deletes is reachable.
The MCP server only reaches the endpoints in ALLOWED, and its results go
through strip_private(), whatever the transport: a model reads them either way.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import date, datetime
from collections.abc import Collection
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from garmin_reader.auth import GarminLoginError, is_refresh_unavailable

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
# Getters taking a date range can make one Garmin request per part of it
# (get_sleep_daily: one per 28 days; get_activities_by_date: one per page),
# so the span is checked before they're called: the result-size cap only
# applies once every request has been made. Same limit as get_garmin_summary.
MAX_RANGE_DAYS = 366
# (start, end) parameter names garminconnect uses for date ranges.
_RANGE_PARAMS = (("start", "end"), ("startdate", "enddate"), ("fromdate", "todate"))
# Getters for which a missing end means the start day alone (they set
# end = start). For any other getter a missing end is assumed to mean today,
# which is right for get_activities_by_date and the safe guess for a getter
# not reviewed yet. tests/test_garmin_library.py checks this list.
MISSING_END_IS_START = frozenset({
    "get_blood_pressure", "get_body_battery", "get_body_composition",
    "get_device_solar_data", "get_endurance_score", "get_hill_score",
})
# Lists longer than this are intraday series (per-minute movement, HRV
# readings, ...); the default day summary drops them and keeps scalars.
SUMMARY_MAX_LIST = 20

# Every getter on garminconnect.Garmin is in exactly one of ALLOWED and
# DENIED (tests/test_exposure.py checks), so a getter added to the library
# later is blocked until someone decides on it here. See
# docs/http-server-design.md, section 5.
#
# Endpoints (without `get_`) the MCP tools may reach. strip_private() is what
# keeps location and identity out of their results.
ALLOWED = frozenset({
    # Daily and weekly health
    "acclimation_stats", "all_day_events", "all_day_stress", "body_battery",
    "body_battery_events", "calories_daily", "daily_body_battery_stats",
    "daily_floors_stats", "daily_heart_rate_stats", "daily_hydration_stats",
    "daily_intensity_minutes_stats", "daily_movement",
    "daily_respiration_stats", "daily_sleep", "daily_stats", "daily_steps",
    "daily_stress_stats", "daily_summaries", "floors", "health_snapshot",
    "health_snapshot_epochs", "health_snapshots", "health_snapshots_range",
    "heart_rates", "hourly_intensity_minutes", "hrv_data", "hrv_data_range",
    "hydration_all_data", "hydration_data", "intensity_minutes_data",
    "monthly_steps", "monthly_wellness_stats", "naps", "respiration_data",
    "rhr_daily", "rhr_day", "sleep_daily", "sleep_data",
    "sleep_stats_overall", "sleep_stats_weekly", "spo2_acclimation_data",
    "spo2_data", "stats", "stats_and_body", "stats_averages", "steps_data",
    "stress_data", "week_average_resting_hr", "weekly_calories_stats",
    "weekly_floors_stats", "weekly_heart_rate_stats",
    "weekly_hydration_stats", "weekly_intensity_minutes", "weekly_steps",
    "weekly_stress", "weekly_wellness_stats",
    # Body
    "biometric_stats", "blood_pressure", "blood_pressure_day",
    "body_composition", "current_biometrics", "daily_last_blood_pressure",
    "daily_last_weight", "daily_weigh_ins", "ecg", "ecg_daily", "ecg_list",
    "first_weight", "glucose_data", "glucose_data_range", "latest_weight",
    "weekly_blood_pressure", "weekly_weight", "weigh_ins", "weight_goal",
    "weight_snapshot",
    # Women's health
    "menstrual_calendar_data", "menstrual_cycle_summary",
    "menstrual_data_for_date", "menstrual_last_confirmed",
    "menstrual_reports", "pregnancy_summary",
    # Training
    "critical_swim_speed", "critical_swim_speed_range", "cycling_ability",
    "cycling_ability_range", "cycling_ftp", "daily_training_status",
    "endurance_score", "fitness_age_daily", "fitness_age_details",
    "fitness_age_weekly", "fitnessage_data",
    "functional_threshold_power_range", "heart_rate_zones",
    "heart_rate_zones_v2", "hill_score", "lactate_threshold", "max_metrics",
    "max_metrics_latest", "max_metrics_range", "morning_training_readiness",
    "one_rep_max", "one_rep_max_history", "power_curve",
    "power_to_weight_range", "power_zones", "power_zones_for_sport",
    "progress_summary_between_dates", "race_predictions", "running_economy",
    "running_economy_range", "running_tolerance", "stamina_curve",
    "strength_benchmarks", "training_four_week_load_balance",
    "training_readiness", "training_readiness_range", "training_status",
    "weekly_training_status",
    # Activities (completed sessions)
    "activities", "activities_by_date", "activities_fordate", "activity",
    "activity_event_types", "activity_exercise_sets",
    "activity_hr_in_timezones", "activity_jump_details", "activity_laps",
    "activity_power_curve", "activity_power_in_timezones",
    "activity_rounds", "activity_split_summaries", "activity_splits",
    "activity_step_details", "activity_typed_splits", "activity_types",
    "adjacent_activities", "fastest_splits", "favorite_activities",
    "last_activity", "personal_record", "personal_record_candidates",
    "personal_record_types", "personal_records_for_activity",
    "training_load_activities", "weekly_activity_stats",
    # Workouts and training plans
    "active_training_plans", "adaptive_training_plan_by_id",
    "next_scheduled_workout", "pace_bands", "scheduled_workout_by_id",
    "scheduled_workouts", "scheduled_workouts_by_period",
    "training_plan_by_id", "training_plan_tasks", "training_plans",
    "workout_by_id", "workout_count", "workout_sport_types",
    "workout_types", "workouts",
    # Nutrition
    "calorie_summary", "lifestyle_logging_data", "nutrition_daily_food_log",
    "nutrition_daily_meals", "nutrition_daily_settings", "nutrition_status",
    # Devices and gear (IDs, names and serial numbers are stripped)
    "active_devices", "activity_gear", "activity_gear_v2", "all_devices",
    "device_alarms", "device_last_used", "devices",
    "gear_activities", "gear_details", "gear_list", "gear_make_models",
    "gear_stats", "gear_types", "historical_devices",
    "primary_training_device", "sensors", "sync_timestamp",
    "user_gear_types",
    # Badges and challenges
    "available_badge_challenges", "available_badges",
    "available_virtual_challenges", "badge_attributes", "badge_challenge",
    "badge_challenges", "badge_detail", "badges_for_activity",
    "badges_for_day", "completed_virtual_challenges", "earned_badges",
    "in_progress_badges", "inprogress_virtual_challenges",
    "latest_earned_badges", "non_completed_badge_challenges",
    # Golf
    "golf_club_stats", "golf_scorecard", "golf_summary", "golf_user_stats",
    # Goals, totals and settings
    "available_metrics", "calendar_year", "effective_goals", "goals",
    "lifetime_totals", "privacy_settings", "report_status", "steps_goal",
    "unit_system", "usage_indicators", "user_goals", "user_statistics",
    "user_summary", "user_timezone",
})

# Endpoints the tools never reach, and why. Only those whose substance
# stripping can't remove belong here: a GPS track or a named place stripped
# of its location is empty.
_LOCATION = "its substance is location: a GPS track, coordinates or named places"
_TRAVEL = "reveals travel: trips, destinations or where the owner has been"
_EVENTS = "race events are named after where and when they happen"
_IDENTITY = "its substance is the owner's identity: name, birth date, e-mail"
_OTHERS = "other people's data (friends' challenges)"
_NEEDS_DEVICE_ID = "needs a device ID, which results never carry"
_NEEDS_PROFILE_ID = "needs the Garmin profile number, which results never carry; use gear_list"
DENIED = {
    "activity_details": _LOCATION,
    "activity_subsecond_details": _LOCATION,
    "activity_swim_details": _LOCATION,
    "activity_map_details": _LOCATION,
    "activity_polyline": _LOCATION,
    "activity_weather": _LOCATION,
    "activity_segments": _LOCATION,
    "activity_images": "photos taken during an activity show where it was",
    "course": _LOCATION,
    "courses": _LOCATION,
    "course_favorites": _LOCATION,
    "golf_shot_data": _LOCATION,
    "jetlag_status": _TRAVEL,
    "jetlag_trips": _TRAVEL,
    "heat_altitude_acclimation": _TRAVEL,
    "heat_altitude_acclimation_range": _TRAVEL,
    "calendar_event": _EVENTS,
    "calendar_events": _EVENTS,
    "calendar_week": _EVENTS,
    "upcoming_events": _EVENTS,
    "primary_event": _EVENTS,
    "full_name": _IDENTITY,
    "user_profile": _IDENTITY,
    "user_profile_base": _IDENTITY,
    "userprofile_settings": _IDENTITY,
    "personal_information": _IDENTITY,
    "adhoc_challenges": _OTHERS,
    "active_adhoc_challenges": _OTHERS,
    "device_app_info": _NEEDS_DEVICE_ID,
    "device_displayed_activities": _NEEDS_DEVICE_ID,
    "device_settings": _NEEDS_DEVICE_ID,
    "device_solar_data": _NEEDS_DEVICE_ID,
    "device_solar_gain": _NEEDS_DEVICE_ID,
    "user_device": _NEEDS_DEVICE_ID,
    # Its fields are free-form: a pushed workout's title arrives as
    # `messageName`, and nothing in it is health data.
    "device_messages": "the watch's message queue: titles of pushed workouts, no health data",
    "gear": _NEEDS_PROFILE_ID,
    "gear_defaults": _NEEDS_PROFILE_ID,
}

# Tool results drop every key whose name contains one of these words:
# coordinates, GPS tracks, altitude and place names reveal where the owner
# lives and trains; names, profile photos, e-mail, actual age and birth date
# identify them (Garmin's fitness age, `fitnessAge`, is kept); activity and
# course names (Garmin names activities after the place, e.g. "Poznan
# Running") and free text (descriptions; notes on gear, blood pressure
# readings and cycle logs) can contain either. Keys are split
# into words (camelCase or snake_case) and compared whole, so e.g.
# `averageOxygen` doesn't match `geo`. Elevation (gain, loss) is kept: it's
# how hilly a workout was, not where.
PRIVATE_KEY_WORDS = frozenset({
    "latitude", "longitude", "lat", "lon", "lng", "gps", "polyline", "geo",
    "location", "altitude",
    "owner", "fullname", "displayname", "email", "description", "notes",
    # Calendar items' `title` is the activity's name ("Poznan Running").
    "title",
    "chronological", "birth", "birthdate", "birthday",
    "serial",
})
# ...or one of these consecutive word pairs. The IDs of the owner's Garmin
# profile and devices (and devices' names, serial numbers and settings files)
# identify them too; product images say nothing about their health. A
# device's model (`deviceTypeSimpleName`) is kept.
PRIVATE_KEY_PHRASES = frozenset({
    ("full", "name"), ("display", "name"), ("user", "name"), ("profile", "image"),
    ("user", "info"), ("activity", "name"), ("course", "name"), ("workout", "name"),
    ("user", "id"), ("user", "profile"), ("profile", "id"), ("profile", "pk"),
    ("device", "id"), ("unit", "id"), ("device", "name"), ("image", "url"),
    ("settings", "file"), ("custom", "make"),  # customMakeModel: a gear nickname
    ("message", "name"),  # a pushed workout's title, queued on a device
    ("user", "roles"),  # the account's permission scopes
})
# ...or one naming a user's ID: user...Id / user...Pk. Garmin keys IDs that
# way, and some are the profile ID under another name (`userDailySummaryId`);
# the rest (`userBadgeId`, `userGoalPk`) are only record numbers.
_USER_ID_ENDINGS = frozenset({"id", "pk"})
# Keys private only in some endpoints' results: there, `name` is the
# nickname the owner gave a piece of gear ("Jane's bike"), while elsewhere
# it's data (lifestyle log entries, badges, exercises).
ENDPOINT_PRIVATE_KEYS = {
    "get_gear_list": frozenset({"name"}),
    "get_gear_details": frozenset({"name"}),
    "get_activity_gear_v2": frozenset({"name"}),
}
# Device IDs also turn up as dict keys (training status is keyed by device);
# strip_private() renames those "device", "device 2", ...
_DEVICE_KEY = "device"
_MIN_DEVICE_ID_DIGITS = 6


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
    touched, so a rejected call makes no Garmin request. Keys private to
    this endpoint (ENDPOINT_PRIVATE_KEYS) are dropped; strip_private() does
    the rest.
    """
    getters = _getters()
    if not _is_allowed(endpoint, allowed):
        raise ValueError(
            f"Endpoint {endpoint!r} is not available in garmin-reader. "
            "Use list_garmin_endpoints to see what is."
        )
    if endpoint not in getters:
        raise ValueError(
            f"Unknown or non-readable endpoint {endpoint!r}. "
            "Use list_garmin_endpoints to see what is available."
        )
    try:
        bound = inspect.signature(getters[endpoint]).bind(api, **(args or {}))
    except TypeError as err:
        raise ValueError(f"Bad arguments for {endpoint}: {err}") from err
    _check_span(endpoint, getters[endpoint], bound.arguments)
    result = getattr(api, endpoint)(**(args or {}))
    return _drop_keys(result, ENDPOINT_PRIVATE_KEYS.get(endpoint, frozenset()))


def _drop_keys(value: Any, keys: frozenset[str]) -> Any:
    if not keys:
        return value
    if isinstance(value, dict):
        return {k: _drop_keys(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [_drop_keys(v, keys) for v in value]
    return value


def _check_span(endpoint: str, fn, arguments: dict) -> None:
    """Refuse a date range longer than MAX_RANGE_DAYS. Only getters with both
    halves of a pair count: a lone `start` is a paging offset (get_goals,
    get_workouts, ...). A missing end means the start day for the getters in
    MISSING_END_IS_START, and today for the rest ("from 1900" with no end is
    the whole history for get_activities_by_date).
    """
    params = inspect.signature(fn).parameters
    for start_name, end_name in _RANGE_PARAMS:
        if start_name not in params or end_name not in params:
            continue
        if arguments.get(start_name) is None:
            continue  # optional range left out (e.g. get_race_predictions)
        start = _date_arg(endpoint, start_name, arguments[start_name])
        end_value = arguments.get(end_name)
        if end_value:
            end = _date_arg(endpoint, end_name, end_value)
        else:
            end = start if endpoint in MISSING_END_IS_START else date.today()
        days = (end - start).days + 1
        if days > MAX_RANGE_DAYS:
            raise ValueError(
                f"{endpoint}: the range is {days} days; at most {MAX_RANGE_DAYS} can be "
                f"requested at once. Split it into smaller ranges (for one day, give "
                f"{end_name} the same date as {start_name})."
            )


def _date_arg(endpoint: str, name: str, value) -> date:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{endpoint}: {name} must be a date, YYYY-MM-DD: {value!r}") from None


def to_json(value: Any) -> str:
    """Compact JSON, the form results are measured and sent in. Keys whose
    value is null are left out: to a model a missing key says the same, and
    Garmin fills responses with them (a strength session's sets carry 24
    velocity and power fields, null without a velocity sensor: three
    quarters of the response).
    """
    return json.dumps(_drop_nulls(value), default=str, separators=(",", ":"))


def _drop_nulls(value: Any) -> Any:
    """Drop null dict values, recursively. Nulls in lists stay: there they
    hold a place, e.g. a day without data in get_garmin_summary's rows.
    """
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value


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
    return (
        bool(PRIVATE_KEY_WORDS.intersection(words))
        or any(pair in PRIVATE_KEY_PHRASES for pair in zip(words, words[1:]))
        or (len(words) > 1 and words[0] == "user" and words[-1] in _USER_ID_ENDINGS)
    )


def strip_private(value: Any) -> Any:
    """Recursively drop keys that are private (see PRIVATE_KEY_WORDS), and
    rename keys that are the IDs of devices named in `value`.
    """
    ids: dict[str, str] = {}
    _collect_device_ids(value, ids)
    return _strip(value, ids)


def _collect_device_ids(value: Any, ids: dict[str, str]) -> None:
    """Map each device ID in `value` to a neutral name, in order of appearance."""
    if isinstance(value, dict):
        for k, v in value.items():
            words = _key_words(str(k))
            if (
                ("device", "id") in zip(words, words[1:])
                and isinstance(v, (int, str)) and not isinstance(v, bool)
                # Garmin's are 10 digits; a small number (a zone, a rank)
                # must never be renamed.
                and len(str(v)) >= _MIN_DEVICE_ID_DIGITS
                and str(v) not in ids
            ):
                n = len(ids) + 1
                ids[str(v)] = _DEVICE_KEY if n == 1 else f"{_DEVICE_KEY} {n}"
            _collect_device_ids(v, ids)
    elif isinstance(value, list):
        for v in value:
            _collect_device_ids(v, ids)


def _strip(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            ids.get(str(k), k): _strip(v, ids)
            for k, v in value.items()
            if not is_private_key(k)
        }
    if isinstance(value, list):
        return [_strip(v, ids) for v in value]
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
            out[m] = {"error": f"Not available in garmin-reader. Available: {sorted(available)}"}
            continue
        if m not in available:
            out[m] = {"error": f"Unknown metric. Available: {sorted(available)}"}
            continue
        try:
            result = getattr(api, f"get_{m}")(day)
            if result is None:
                # No data that day. to_json() leaves nulls out, and a metric
                # that vanished would look ignored rather than empty.
                result = {}
            hint = f'pass metrics=["{m}"] for the full list'
            out[m] = summarize(result, hint=hint) if slim else result
        except (GarminLoginError, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError):
            raise  # affects every metric: fail the call once, don't retry per metric
        except Exception as err:  # one failing metric shouldn't sink the rest
            if is_refresh_unavailable(err):
                raise
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
