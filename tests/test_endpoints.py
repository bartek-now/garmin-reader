import json

import pytest
from garminconnect import GarminConnectConnectionError

from garmin_reader import endpoints


class FakeApi:
    def get_sleep_data(self, cdate):
        return {"date": cdate}

    def get_hrv_data(self, cdate):
        raise RuntimeError("boom")


def test_list_endpoints_only_getters():
    names = {e["endpoint"] for e in endpoints.list_endpoints()}
    assert "get_sleep_data" in names
    assert all(n.startswith("get_") for n in names)


def test_call_rejects_non_getter():
    with pytest.raises(ValueError, match="Unknown"):
        endpoints.call_endpoint(FakeApi(), "download_activity", {"activity_id": "1"})


def test_call_rejects_bad_args():
    with pytest.raises(ValueError, match="Bad arguments"):
        endpoints.call_endpoint(FakeApi(), "get_sleep_data", {"nope": 1})


def test_call_passes_args():
    out = endpoints.call_endpoint(FakeApi(), "get_sleep_data", {"cdate": "2026-10-05"})
    assert out == {"date": "2026-10-05"}


def test_cap_size_replaces_large_result():
    out = endpoints.cap_size({"a": "x" * 400}, limit=300)
    assert "error" in out and out["top_level_keys"] == ["a"]


def test_cap_size_drops_key_list_when_notice_too_big():
    out = endpoints.cap_size({f"key{i}": i for i in range(100)}, limit=200)
    assert "top_level_keys" not in out and len(json.dumps(out)) <= 200


def test_to_json_leaves_out_null_keys_at_any_depth():
    value = {"a": None, "b": {"c": None, "d": 1}, "e": [{"f": None, "g": 2}]}
    assert json.loads(endpoints.to_json(value)) == {"b": {"d": 1}, "e": [{"g": 2}]}


def test_to_json_keeps_nulls_in_lists():
    # get_garmin_summary's rows: null holds the place of a day without data.
    table = {"fields": ["date", "hrv"], "days": [["2026-10-05", None], ["2026-10-06", 41]]}
    assert json.loads(endpoints.to_json(table)) == table


# Shaped like a real get_activity_exercise_sets response for a 123-minute
# strength session (40 working sets, 40 rests): 71k chars as Garmin sends it.
_VELOCITY_FIELDS = [
    agg + field
    for agg in ("avg", "max")
    for field in (
        "ConcentricMeanVelocity", "EccentricMeanVelocity", "ConcentricPeakVelocity",
        "EccentricPeakVelocity", "MeanPropulsiveVelocity", "ConcentricMeanPower",
        "ConcentricPeakPower", "ConcentricDuration", "EccentricDuration", "ConcentricRom",
    )
]
LONG_STRENGTH_SESSION = {
    "activityId": 1,
    "exerciseSets": [
        {
            "exercises": [
                {"category": "TRICEPS_EXTENSION", "name": "BENCH_DIP", "probability": 42.96875},
                {"category": "PUSH_UP", "name": None, "probability": 33.203125},
                {"category": "PULL_UP", "name": None, "probability": 22.265625},
            ] if i % 2 == 0 else [],
            "duration": 16.386, "repetitionCount": 10 if i % 2 == 0 else None,
            "weight": 0.0 if i % 2 == 0 else None,
            "setType": "ACTIVE" if i % 2 == 0 else "REST",
            "startTime": "2026-09-06T07:28:05.0", "wktStepIndex": None,
            "messageIndex": i, "minimalViableThreshold": None,
            **{f: None for f in _VELOCITY_FIELDS},
        }
        for i in range(80)
    ],
}


def test_long_strength_session_fits_once_nulls_are_left_out():
    assert len(json.dumps(LONG_STRENGTH_SESSION, separators=(",", ":"))) > endpoints.MAX_RESULT_CHARS
    out = endpoints.cap_size(LONG_STRENGTH_SESSION)
    assert out is LONG_STRENGTH_SESSION  # not replaced by the too-large notice
    sent = endpoints.to_json(out)
    assert len(sent) < endpoints.MAX_RESULT_CHARS / 2
    sets = json.loads(sent)["exerciseSets"]
    assert len(sets) == 80
    assert sets[0]["exercises"][1] == {"category": "PUSH_UP", "probability": 33.203125}
    assert sets[1] == {"exercises": [], "duration": 16.386, "setType": "REST",
                       "startTime": "2026-09-06T07:28:05.0", "messageIndex": 1}


def test_day_metric_without_data_stays_visible():
    class NoDataApi:
        def get_sleep_data(self, cdate):
            return None

    out = endpoints.day_metrics(NoDataApi(), "2026-10-05", ["sleep_data"])
    assert json.loads(endpoints.to_json(out)) == {"date": "2026-10-05", "sleep_data": {}}


def test_day_metrics_dedupes():
    class CountingApi:
        calls = 0

        def get_sleep_data(self, cdate):
            CountingApi.calls += 1
            return {}

    endpoints.day_metrics(CountingApi(), "d", ["sleep_data", "sleep_data"])
    assert CountingApi.calls == 1


def test_day_metrics_isolates_errors():
    out = endpoints.day_metrics(FakeApi(), "2026-10-05", ["sleep_data", "hrv_data", "bogus"])
    assert out["sleep_data"] == {"date": "2026-10-05"}
    assert "boom" in out["hrv_data"]["error"]
    assert "Unknown metric" in out["bogus"]["error"]


def test_unavailable_endpoint_does_not_look_like_refresh_failure():
    class Api(FakeApi):
        def get_hrv_data(self, cdate):
            raise GarminConnectConnectionError("API Error 404: no HRV on this watch")

    out = endpoints.day_metrics(Api(), "2026-10-05", ["hrv_data", "sleep_data"])
    assert out == {
        "date": "2026-10-05",
        "hrv_data": {"error": "GarminConnectConnectionError: API Error 404: no HRV on this watch"},
        "sleep_data": {"date": "2026-10-05"},
    }


def test_default_day_metrics_are_valid():
    assert set(endpoints.DEFAULT_DAY_METRICS) <= set(endpoints.single_day_metrics())


@pytest.mark.parametrize(
    "name",
    ["add_weigh_in", "delete_weigh_ins", "upload_activity", "set_blood_pressure",
     "download_activity", "__class__", "login", "garth"],
)
def test_real_garmin_rejects_non_getters(name):
    from garminconnect import Garmin

    with pytest.raises(ValueError, match="Unknown"):
        endpoints.call_endpoint(Garmin("a", "b"), name, {})


def test_no_getter_name_looks_like_a_write():
    names = {e["endpoint"] for e in endpoints.list_endpoints()}
    assert not [n for n in names if any(w in n for w in ("add_", "delete", "upload", "set_"))]


def test_summarize_drops_long_lists_keeps_scalars():
    data = {"score": 80, "series": list(range(100)), "nested": {"short": [1, 2], "long": [{}] * 50}}
    out = endpoints.summarize(data)
    assert out["score"] == 80 and out["nested"]["short"] == [1, 2]
    assert "100 items omitted" in out["series"]
    assert "50 items omitted" in out["nested"]["long"]


class BigApi:
    def get_sleep_data(self, cdate):
        return {"summary": 1, "movement": list(range(5000))}

    def get_hrv_data(self, cdate):
        return {"x": "y" * 300_000}


def test_default_day_metrics_are_summarized_and_total_capped():
    api = BigApi()
    # explicit request keeps raw series
    raw = endpoints.day_metrics(api, "d", ["sleep_data"])
    assert len(raw["sleep_data"]["movement"]) == 5000
    # total cap replaces the oversized metric
    capped = endpoints.day_metrics(api, "d", ["sleep_data", "hrv_data"])
    assert "too large to return even on its own" in capped["hrv_data"]["error"]
    assert capped["sleep_data"]["summary"] == 1


def test_default_omission_marker_says_how_to_get_full_list():
    out = endpoints.day_metrics(BigApi(), "d")
    assert out["sleep_data"]["movement"] == (
        '<5000 items omitted; pass metrics=["sleep_data"] for the full list>'
    )


def test_empty_metrics_selection_stays_empty():
    assert endpoints.day_metrics(FakeApi(), "d", []) == {"date": "d"}


def test_total_cap_counts_keys_and_punctuation():
    out = endpoints._fit_total({"a": "x" * 50, "b": "y" * 50}, limit=100)
    assert len(json.dumps(out)) <= 100


def test_total_cap_falls_back_when_keys_alone_too_big():
    out = endpoints._fit_total({"k" * 500: 1}, limit=100)
    assert set(out) == {"error"}


def test_cap_size_list_notice_has_item_count():
    out = endpoints.cap_size(["x" * 100] * 10, limit=500)
    assert out["items"] == 10 and "top_level_keys" not in out


def test_total_cap_suggests_requesting_alone_when_that_fits():
    out = endpoints._fit_total({"a": "x" * 400, "b": "y" * 400}, limit=600)
    omitted = [v for v in out.values() if isinstance(v, dict)]
    assert omitted and "request this metric on its own" in omitted[0]["error"]


def test_total_cap_counts_date_and_key_in_solo_size():
    # The value alone fits in 300 chars, but {"date":..., "a": value} doesn't.
    out = endpoints._fit_total({"date": "2026-10-05", "a": "x" * 280, "b": 1}, limit=300)
    assert "too large to return even on its own" in str(out)


# --- date-range span ----------------------------------------------------------------


class Recorder:
    """Records each getter called; returns an empty result."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def getter(*args, **kwargs):
            self.calls.append(name)
            return {}
        return getter


def test_long_range_refused_before_any_request():
    # get_sleep_daily makes one request per 28 days: 1900-2026 would be
    # over 1,600 requests before the result-size cap could apply.
    api = Recorder()
    with pytest.raises(ValueError, match="at most 366"):
        endpoints.call_endpoint(api, "get_sleep_daily", {"start": "1900-01-01", "end": "2026-10-01"})
    assert api.calls == []


def test_a_year_is_allowed():
    api = Recorder()
    endpoints.call_endpoint(api, "get_sleep_daily", {"start": "2025-10-01", "end": "2026-10-01"})
    assert api.calls == ["get_sleep_daily"]


def test_missing_end_means_today():
    from datetime import date, timedelta

    api = Recorder()
    with pytest.raises(ValueError, match="at most 366"):
        endpoints.call_endpoint(api, "get_activities_by_date", {"startdate": "1900-01-01"})
    recent = (date.today() - timedelta(days=30)).isoformat()
    endpoints.call_endpoint(api, "get_activities_by_date", {"startdate": recent})
    assert api.calls == ["get_activities_by_date"]


@pytest.mark.parametrize("endpoint, args", [
    ("get_workouts", {"start": 0, "limit": 10}),  # paging offsets, not dates
    ("get_goals", {"start": "1"}),
    ("get_race_predictions", {}),  # optional range left out
])
def test_non_date_starts_are_not_ranges(endpoint, args):
    api = Recorder()
    endpoints.call_endpoint(api, endpoint, args)
    assert api.calls == [endpoint]


def test_range_dates_must_be_dates():
    api = Recorder()
    with pytest.raises(ValueError, match="start must be a date"):
        endpoints.call_endpoint(api, "get_sleep_daily", {"start": "last year", "end": "2026-10-01"})
    assert api.calls == []


@pytest.mark.parametrize("endpoint", sorted(endpoints.MISSING_END_IS_START))
def test_old_single_day_allowed_where_missing_end_means_that_day(endpoint):
    api = Recorder()
    endpoints.call_endpoint(api, endpoint, {"startdate": "2020-01-01"} if endpoint != "get_device_solar_data"
                            else {"device_id": "1", "startdate": "2020-01-01"})
    assert api.calls == [endpoint]


def test_span_error_explains_how_to_ask_for_one_day():
    with pytest.raises(ValueError, match="give enddate the same date as startdate"):
        endpoints.call_endpoint(Recorder(), "get_activities_by_date", {"startdate": "2020-01-01"})
