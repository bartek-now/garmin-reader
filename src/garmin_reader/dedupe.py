"""Build a DayStats from raw Garmin responses.

Garmin's daily step total already includes steps taken during
step-based workouts (runs, walks, hikes). Any activity that reports a
`steps` count is treated as a workout, and its steps are subtracted
from the daily total so a step is never counted both as "background"
movement and as part of a workout.
"""

from __future__ import annotations

from garmin_reader.models import DayStats, Workout


def _latest_entry(weigh_in: dict) -> dict | None:
    """The last weigh-in of the day (by timestamp), or None if the scale
    wasn't used that day.
    """
    entries = weigh_in.get("dateWeightList") or []
    if not entries:
        return None
    return max(entries, key=lambda e: e.get("date") or e.get("timestampGMT") or 0)


def _grams_to_kg(grams: float | None) -> float | None:
    return round(grams / 1000, 1) if grams is not None else None


def _latest_weight_kg(weigh_in: dict) -> float | None:
    """Weight from the last weigh-in of the day; Garmin reports grams."""
    latest = _latest_entry(weigh_in)
    return _grams_to_kg(latest.get("weight")) if latest else None


def build_weight_row(date_str: str, weigh_in: dict) -> dict | None:
    """Compact body composition from the day's last weigh-in, or None if
    there was none. Fields the scale didn't report are left out; `count`
    appears only when there were several weigh-ins that day.
    """
    latest = _latest_entry(weigh_in)
    if latest is None or latest.get("weight") is None:
        return None
    row = {
        "date": date_str,
        "weight_kg": _grams_to_kg(latest["weight"]),
        "body_fat_pct": latest.get("bodyFat"),
        "muscle_mass_kg": _grams_to_kg(latest.get("muscleMass")),
    }
    count = len(weigh_in.get("dateWeightList") or [])
    if count > 1:
        row["count"] = count
    return {k: v for k, v in row.items() if v is not None}


def build_day_stats(
    date_str: str, summary: dict, activities: list[dict], weigh_in: dict | None = None
) -> DayStats:
    workouts = [Workout.from_raw(a) for a in activities]
    return DayStats(
        date=date_str,
        weight_kg=_latest_weight_kg(weigh_in) if weigh_in else None,
        total_steps=summary.get("totalSteps") or 0,
        total_calories=summary.get("totalKilocalories") or 0.0,
        active_calories=summary.get("activeKilocalories") or 0.0,
        bmr_calories=summary.get("bmrKilocalories") or 0.0,
        workouts=workouts,
    )
