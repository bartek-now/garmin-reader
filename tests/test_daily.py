from datetime import date

import pytest

from kcal.daily import fetch_days, resolve_days

TODAY = date(2026, 7, 9)
YESTERDAY = date(2026, 7, 8)


def test_neither_given_defaults_to_yesterday():
    assert resolve_days(None, None, None, today=TODAY) == [YESTERDAY]


def test_date_alone_returns_single_day():
    assert resolve_days("2026-07-01", None, None, today=TODAY) == [date(2026, 7, 1)]


def test_from_alone_ranges_through_yesterday():
    days = resolve_days(None, "2026-07-05", None, today=TODAY)
    assert days == [date(2026, 7, 5), date(2026, 7, 6), date(2026, 7, 7), YESTERDAY]


def test_from_and_to_given_uses_explicit_range():
    days = resolve_days(None, "2026-07-05", "2026-07-06", today=TODAY)
    assert days == [date(2026, 7, 5), date(2026, 7, 6)]


def test_to_without_from_errors():
    with pytest.raises(ValueError, match="end date needs a start date"):
        resolve_days(None, None, "2026-07-06", today=TODAY)


def test_date_combined_with_from_errors():
    with pytest.raises(ValueError, match="single date or a start/end range"):
        resolve_days("2026-07-01", "2026-07-05", None, today=TODAY)


def test_date_combined_with_to_errors():
    with pytest.raises(ValueError, match="single date or a start/end range"):
        resolve_days("2026-07-01", None, "2026-07-06", today=TODAY)


def test_to_before_from_errors():
    with pytest.raises(ValueError, match="End date 2026-07-01 is before start date 2026-07-06"):
        resolve_days(None, "2026-07-06", "2026-07-01", today=TODAY)


def test_bad_date_format_errors():
    with pytest.raises(ValueError, match="Invalid date '2026-13-01'; expected YYYY-MM-DD"):
        resolve_days("2026-13-01", None, None, today=TODAY)


def test_range_over_max_days_errors():
    with pytest.raises(ValueError, match="Range is 5 days; at most 4"):
        resolve_days(None, "2026-07-01", "2026-07-05", today=TODAY, max_days=4)


def test_range_at_max_days_ok():
    assert len(resolve_days(None, "2026-07-01", "2026-07-04", today=TODAY, max_days=4)) == 4


def test_huge_range_rejected_without_building_it(monkeypatch):
    monkeypatch.setattr("kcal.daily.timedelta", None)  # building the list would fail
    with pytest.raises(ValueError, match="at most 120"):
        resolve_days(None, "0001-01-01", "9999-12-31", today=TODAY, max_days=120)


def test_single_date_ignores_max_days():
    assert resolve_days("2026-07-01", None, None, today=TODAY, max_days=0) == [date(2026, 7, 1)]


class RangeApi:
    def __init__(self):
        self.calls = []

    def get_user_summary(self, cdate):
        self.calls.append("summary")
        return {"totalSteps": 1000}

    def get_activities_by_date(self, startdate, enddate):
        self.calls.append("activities")
        return [
            {"activityId": 1, "startTimeLocal": "2026-07-02 08:00:00", "steps": 300},
        ]

    def get_weigh_ins(self, startdate, enddate):
        self.calls.append("weigh_ins")
        return {
            "dailyWeightSummaries": [
                {
                    "summaryDate": "2026-07-01",
                    "allWeightMetrics": [{"date": 1, "weight": 70000.0}],
                }
            ]
        }


def test_fetch_days_batches_activities_and_weigh_ins():
    api = RangeApi()
    days = resolve_days(None, "2026-07-01", "2026-07-03", today=TODAY)
    stats = fetch_days(days, login_fn=lambda: api)
    assert sorted(api.calls) == ["activities", "summary", "summary", "summary", "weigh_ins"]
    assert [s.weight_kg for s in stats] == [70.0, None, None]
    assert [len(s.workouts) for s in stats] == [0, 1, 0]
    assert stats[1].non_workout_steps == 700


def test_fetch_days_empty():
    assert fetch_days([], login_fn=lambda: pytest.fail("should not log in")) == []
