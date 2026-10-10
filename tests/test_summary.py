"""get_garmin_summary's data assembly (kcal.summary), with a fake Garmin."""

import contextvars
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from kcal import summary

D1, D2, D3 = date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)


def local_millis(iso: str) -> int:
    """Garmin's "local" millis: wall-clock time encoded as if UTC."""
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


class FakeGarmin:
    """Every source summary uses, recording what was asked for."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def _record(self, *call):
        with self.lock:
            self.calls.append(call)

    def get_user_summary(self, cdate):
        self._record("user_summary", cdate)
        return {
            "totalSteps": 10000, "activeKilocalories": 400.4, "bmrKilocalories": 2000.6,
            "moderateIntensityMinutes": 20, "vigorousIntensityMinutes": 5,
            "floorsAscended": 12.4, "restingHeartRate": 46,
            "lastSevenDaysAvgRestingHeartRate": 47, "minHeartRate": 44, "maxHeartRate": 150,
            "averageStressLevel": 25 if cdate != str(D1) else -1, "maxStressLevel": 90,
            "bodyBatteryHighestValue": 88, "bodyBatteryLowestValue": 20,
            "bodyBatteryChargedValue": 60, "bodyBatteryDrainedValue": 55,
            "bodyBatteryAtWakeTime": 80,
        }

    def get_activities_by_date(self, start, end):
        self._record("activities", start, end)
        return [{
            "activityId": 1, "activityName": "Run", "activityType": {"typeKey": "running"},
            "startTimeLocal": f"{D2} 07:30:00", "duration": 1800.0, "calories": 400.0,
            "bmrCalories": 40.0, "steps": 4000,
        }]

    def get_weigh_ins(self, start, end):
        self._record("weigh_ins", start, end)
        return {"dailyWeightSummaries": [{
            "summaryDate": str(D2),
            "allWeightMetrics": [
                {"date": 1, "weight": 77000.0, "bodyFat": 22.0, "muscleMass": 31000.0},
                {"date": 2, "weight": 76500.0, "bodyFat": 22.4, "muscleMass": 31100.0},
            ],
        }]}

    def get_lifestyle_logging_data(self, cdate):
        self._record("lifestyle", cdate)
        return {"dailyLogsReport": [
            {"name": "Late Meals", "logStatus": "YES"},
            {"name": "Heavy Meals", "logStatus": "NO"},
            {"name": "Eye Mask"},
        ]}

    def connectapi(self, path):
        self._record("connectapi", path)
        start, end = path.rsplit("/", 2)[-2:]
        if path.startswith("/hrv-service/"):
            return {"hrvSummaries": [{
                "calendarDate": str(D3), "lastNightAvg": 41, "lastNight5MinHigh": 66,
                "weeklyAvg": 39, "baseline": {"balancedLow": 41, "balancedUpper": 52},
                "status": "UNBALANCED",
            }]}
        if path.startswith("/sleep-service/"):
            return {"individualStats": [{
                "calendarDate": str(D3), "values": {
                    "sleepScore": 71, "sleepScoreQuality": "FAIR",
                    "localSleepStartTimeInMillis": local_millis(f"{D2}T23:39"),
                    "localSleepEndTimeInMillis": local_millis(f"{D3}T07:22"),
                    "totalSleepTimeInSeconds": 19560, "deepTime": 7620, "lightTime": 9120,
                    "remTime": 2820, "awakeTime": 1020, "avgHeartRate": 51.4,
                    "respiration": 14.23, "spO2": 97.84, "bodyBatteryChange": 43,
                    "sleepNeed": 460,
                },
            }]} if start <= str(D3) <= end else {"individualStats": []}
        if path.startswith("/metrics-service/"):
            return [
                {"calendarDate": str(D3), "inputContext": "AFTER_POST_EXERCISE_RESET",
                 "timestamp": "2026-10-09T15:00:00", "score": 40, "level": "LOW"},
                {"calendarDate": str(D3), "inputContext": "AFTER_WAKEUP_RESET",
                 "timestamp": "2026-10-09T07:30:00", "score": 61, "level": "MODERATE",
                 "acuteLoad": 184, "recoveryTime": 1},
                {"calendarDate": str(D2), "inputContext": "UPDATE_REALTIME_VARIABLES",
                 "timestamp": "2026-10-08T12:00:00", "score": 55, "level": "MODERATE"},
                {"calendarDate": str(D2), "inputContext": "AFTER_POST_EXERCISE_RESET",
                 "timestamp": "2026-10-08T09:00:00", "score": 50, "level": "MODERATE"},
            ]
        raise AssertionError(path)


@pytest.fixture
def api():
    return FakeGarmin()


def table(api, groups, days=(D1, D2, D3)):
    out = summary.summarize(api, list(days), groups)
    return out["fields"], {row[0]: dict(zip(out["fields"], row)) for row in out["days"]}


# --- each group --------------------------------------------------------------------


def test_activity(api):
    _, rows = table(api, ["activity"])
    d2 = rows[str(D2)]
    assert d2["steps"] == 10000 and d2["non_workout_steps"] == 6000  # workout steps removed
    assert d2["active_calories"] == 400 and d2["passive_calories"] == 2001
    assert d2["workout_calories"] == 400 and d2["workout_active_calories"] == 360
    assert d2["intensity_minutes_moderate"] == 20 and d2["floors_climbed"] == 12
    assert rows[str(D1)]["workout_calories"] == 0


def test_heart_and_stress(api):
    _, rows = table(api, ["heart", "stress"])
    assert rows[str(D2)]["resting_hr"] == 46 and rows[str(D2)]["max_hr"] == 150
    assert rows[str(D2)]["stress_avg"] == 25 and rows[str(D2)]["body_battery_at_wake"] == 80
    assert rows[str(D1)]["stress_avg"] is None  # Garmin's -1: not enough data


def test_lifestyle(api):
    _, rows = table(api, ["lifestyle"])
    assert rows[str(D2)]["logged_yes"] == ["Late Meals"]
    assert rows[str(D2)]["logged_no"] == ["Heavy Meals"]


def test_weight(api):
    fields, rows = table(api, ["weight"])
    assert fields == ["date", "weight_kg", "body_fat_pct", "muscle_mass_kg", "weigh_ins"]
    assert rows == {str(D2): {"date": str(D2), "weight_kg": 76.5, "body_fat_pct": 22.4,
                              "muscle_mass_kg": 31.1, "weigh_ins": 2}}


def test_workouts_are_compact(api):
    _, rows = table(api, ["workouts"])
    assert rows[str(D2)]["workouts"] == [{
        "id": 1, "name": "Run", "type": "running", "start": "07:30", "minutes": 30,
        "calories": 400, "active_calories": 360, "steps": 4000,
    }]


def test_hrv(api):
    _, rows = table(api, ["hrv"])
    assert rows[str(D3)] == {
        "date": str(D3), "hrv_last_night_ms": 41, "hrv_last_night_5min_high_ms": 66,
        "hrv_weekly_avg_ms": 39, "hrv_baseline_low_ms": 41, "hrv_baseline_high_ms": 52,
        "hrv_status": "UNBALANCED",
    }


def test_sleep(api):
    _, rows = table(api, ["sleep"])
    night = rows[str(D3)]
    assert night["sleep_start"] == "23:39" and night["sleep_end"] == "07:22"
    assert night["sleep_minutes"] == 326 and night["deep_sleep_minutes"] == 127
    assert night["awake_minutes"] == 17 and night["sleep_avg_hr"] == 51
    assert night["sleep_respiration"] == 14.2 and night["sleep_spo2"] == 97.8
    assert night["sleep_score"] == 71 and night["sleep_need_minutes"] == 460


def test_readiness_prefers_the_morning_assessment(api):
    _, rows = table(api, ["readiness"])
    assert rows[str(D3)]["readiness_score"] == 61  # AFTER_WAKEUP_RESET, not the later 40
    assert rows[str(D3)]["acute_training_load"] == 184
    assert rows[str(D2)]["readiness_score"] == 50  # no morning one: the earliest


# --- the table -----------------------------------------------------------------------


def test_columns_follow_the_requested_group_order(api):
    fields, _ = table(api, ["hrv", "weight"])
    assert fields[0] == "date"
    assert fields.index("hrv_status") < fields.index("weight_kg")
    fields, _ = table(api, ["weight", "hrv"])
    assert fields.index("weight_kg") < fields.index("hrv_status")


def test_missing_values_are_null_and_empty_columns_and_days_dropped(api):
    out = summary.summarize(api, [D1, D2, D3], ["weight", "hrv"])
    assert [row[0] for row in out["days"]] == [str(D2), str(D3)]  # D1 had neither
    fields = out["fields"]
    d2 = dict(zip(fields, out["days"][0]))
    assert d2["hrv_status"] is None and d2["weight_kg"] == 76.5


def test_every_group_field_is_produced_and_documented(api):
    # With every source reporting, every declared column comes out, so none
    # is silently dropped; and each is named in the tool description.
    fields, _ = table(api, list(summary.GROUP_FIELDS))
    expected = [f for g in summary.GROUP_FIELDS.values() for f in g]
    assert fields == ["date", *expected]
    assert set(summary.GROUP_FIELDS) == set(summary.GROUP_DOCS)
    for group, names in summary.GROUP_FIELDS.items():
        doc = summary.GROUP_DOCS[group]
        for name in names:
            # Docs abbreviate siblings ("deep/light/rem_sleep_minutes"), so
            # check every word of the name appears in the group's doc.
            missing = [w for w in name.split("_") if w not in doc]
            assert not missing, (group, name, missing)


# --- fetching ---------------------------------------------------------------------------


def test_only_needed_sources_are_fetched(api):
    table(api, ["hrv"])
    assert api.calls == [("connectapi", f"/hrv-service/hrv/daily/{D1}/{D3}")]


def test_range_groups_cost_one_request(api):
    table(api, ["weight", "workouts", "hrv", "readiness"])
    assert sorted(c[0] for c in api.calls) == ["activities", "connectapi", "connectapi", "weigh_ins"]


def test_sleep_is_fetched_in_28_day_chunks(api):
    days = [D1 + timedelta(days=i) for i in range(60)]
    summary.summarize(api, days, ["sleep"])
    paths = [c[1] for c in api.calls]
    assert paths == [
        f"/sleep-service/stats/sleep/daily/{days[0]}/{days[27]}",
        f"/sleep-service/stats/sleep/daily/{days[28]}/{days[55]}",
        f"/sleep-service/stats/sleep/daily/{days[56]}/{days[59]}",
    ]


def test_per_day_groups_share_one_request_per_day(api):
    table(api, ["activity", "heart", "stress"])
    assert sorted(c for c in api.calls if c[0] == "user_summary") == [
        ("user_summary", str(D1)), ("user_summary", str(D2)), ("user_summary", str(D3)),
    ]


def test_per_day_requests_run_in_parallel_after_the_first(api):
    barrier = threading.Barrier(2, timeout=5)
    seen = []
    original = api.get_user_summary

    def summary_waiting_for_a_peer(cdate):
        seen.append(cdate)
        if cdate != str(D1):  # D1 runs alone first; D2 and D3 must overlap
            barrier.wait()
        return original(cdate)

    api.get_user_summary = summary_waiting_for_a_peer
    _, rows = table(api, ["activity"])
    assert seen[0] == str(D1) and sorted(rows) == [str(D1), str(D2), str(D3)]


def test_per_day_workers_see_the_callers_context(api):
    var = contextvars.ContextVar("policy", default="unset")
    seen = []
    original = api.get_lifestyle_logging_data
    api.get_lifestyle_logging_data = lambda cdate: seen.append(var.get()) or original(cdate)
    var.set("remote")
    table(api, ["lifestyle"])
    assert seen == ["remote"] * 3


# --- range limits ----------------------------------------------------------------------


def span(n):
    return [D1 + timedelta(days=i) for i in range(n)]


def test_range_only_groups_allow_a_year(api):
    summary.check_range(span(summary.MAX_RANGE_DAYS), ["hrv", "sleep", "weight"])
    with pytest.raises(ValueError, match="at most 366"):
        summary.check_range(span(summary.MAX_RANGE_DAYS + 1), ["hrv"])


def test_per_day_groups_limit_the_range(api):
    summary.check_range(span(summary.MAX_PER_DAY_DAYS), ["activity", "hrv"])
    with pytest.raises(ValueError, match="with activity, lifestyle .* at most 120"):
        summary.check_range(span(summary.MAX_PER_DAY_DAYS + 1), ["hrv", "lifestyle", "activity"])


def test_range_endpoint_paths():
    ok = [f"/hrv-service/hrv/daily/{D1}/{D3}", f"/sleep-service/stats/sleep/daily/{D1}/{D3}",
          f"/metrics-service/metrics/trainingreadiness/{D1}/{D3}"]
    bad = ["/userprofile-service/socialProfile", f"/hrv-service/hrv/daily/{D1}/{D3}/../x",
           f"/hrv-service/hrv/daily/{D1}", f"/hrv-service/hrv/daily/{D1}/{D3}?x=1",
           f"//hrv-service/hrv/daily/{D1}/{D3}"]
    assert all(summary.RANGE_PATHS.fullmatch(p) for p in ok)
    assert not any(summary.RANGE_PATHS.fullmatch(p) for p in bad)


# --- review fixes -----------------------------------------------------------------------


def test_days_without_data_are_not_zeros(api):
    original = api.get_user_summary
    api.get_user_summary = lambda cdate: {} if cdate == str(D1) else original(cdate)
    _, rows = table(api, ["activity"])
    assert str(D1) not in rows  # no zeros for a day Garmin has nothing for
    assert rows[str(D2)]["steps"] == 10000


def test_failing_group_reports_its_error_and_others_still_come_back(api):
    original = api.connectapi

    def no_hrv(path):
        if path.startswith("/hrv-service/"):
            raise RuntimeError("404 no HRV on this watch")
        return original(path)

    api.connectapi = no_hrv
    out = summary.summarize(api, [D1, D2, D3], ["weight", "hrv", "sleep"])
    assert out["errors"] == {"hrv": "RuntimeError: 404 no HRV on this watch"}
    assert "weight_kg" in out["fields"] and "sleep_score" in out["fields"]
    assert not any(f.startswith("hrv_") for f in out["fields"])


def test_failing_activities_fail_activity_and_workouts_only(api):
    def broken(start, end):
        raise RuntimeError("activities down")

    api.get_activities_by_date = broken
    out = summary.summarize(api, [D1, D2, D3], ["activity", "workouts", "heart"])
    assert set(out["errors"]) == {"activity", "workouts"}
    assert "resting_hr" in out["fields"] and "steps" not in out["fields"]


def test_failing_day_is_reported_with_a_count(api):
    original = api.get_lifestyle_logging_data

    def flaky(cdate):
        if cdate != str(D2):
            raise RuntimeError("500")
        return original(cdate)

    api.get_lifestyle_logging_data = flaky
    out = summary.summarize(api, [D1, D2, D3], ["lifestyle", "heart"])
    assert out["errors"]["lifestyle"].startswith("2 day(s) failed, e.g. ")
    assert "heart" not in out["errors"]
    fields, rows = out["fields"], {r[0]: dict(zip(out["fields"], r)) for r in out["days"]}
    assert rows[str(D2)]["logged_yes"] == ["Late Meals"]


@pytest.mark.parametrize("error", ["login", "auth"])
def test_login_problems_still_fail_the_whole_call(api, error):
    from garminconnect import GarminConnectAuthenticationError

    from kcal.auth import GarminLoginError

    exc = GarminLoginError("expired") if error == "login" else GarminConnectAuthenticationError("401")

    def fail(path):
        raise exc

    api.connectapi = fail
    with pytest.raises(type(exc)):
        summary.summarize(api, [D1, D2, D3], ["weight", "hrv"])


def test_a_failed_day_stops_the_rest(api):
    from garminconnect import GarminConnectAuthenticationError

    days = [D1 + timedelta(days=i) for i in range(40)]
    asked = []

    def summary_failing_on_day_2(cdate):
        asked.append(cdate)
        if cdate == str(days[1]):
            raise GarminConnectAuthenticationError("401")
        threading.Event().wait(0.05)
        return {"totalSteps": 1}

    api.get_user_summary = summary_failing_on_day_2
    with pytest.raises(GarminConnectAuthenticationError):
        summary.summarize(api, days, ["activity"])
    assert len(asked) < 15  # the queued days were cancelled, not all sent


def test_empty_range_has_the_usual_shape(api):
    assert summary.summarize(api, [], ["hrv"]) == {"fields": ["date"], "days": []}
    assert api.calls == []


def test_token_refresh_is_serialized():
    from kcal.auth import _serialize_token_refresh

    running, overlaps = [], []

    class Client:
        def _refresh_session(self):
            running.append(1)
            if len(running) > 1:
                overlaps.append(1)
            threading.Event().wait(0.05)
            running.pop()

    client = Client()
    _serialize_token_refresh(client)
    _serialize_token_refresh(client)  # idempotent
    threads = [threading.Thread(target=client._refresh_session) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == []
