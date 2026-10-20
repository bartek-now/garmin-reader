"""What the tools may reach, over stdio and HTTP alike (docs/http-server-design.md, section 5)."""

import asyncio
import json

import pytest
from garminconnect import Garmin
from mcp.server.fastmcp.exceptions import ToolError

from garmin_reader import endpoints, mcp_server
from garmin_reader.settings import HttpSettings

GETTERS = {name.removeprefix("get_") for name in endpoints._getters()}
DISALLOWED = sorted(endpoints.DENIED)

# Location or identity is their substance, so stripping can't make them safe.
SENSITIVE = [
    "activity_details", "activity_polyline", "activity_map_details",
    "activity_weather", "course", "jetlag_status", "heat_altitude_acclimation",
    "user_profile", "userprofile_settings", "full_name", "personal_information",
    "device_messages",  # a queued workout's title arrives as `messageName`
]

# Shaped like real get_activities / get_activity responses.
LOCATED = {
    "activityId": 1,
    "locationName": "Somewhere",
    "summaryDTO": {"calories": 500.0, "startLatitude": 52.1, "endLongitude": 21.0},
    "lapDTOs": [{"distance": 1000, "startLatitude": 52.1, "startLongitude": 21.0}],
    "geoPolylineDTO": {"polyline": [[52.1, 21.0]]},
    "hasPolyline": True,
    "ownerFullName": "Jane Doe",
    "activityName": "Poznan Running",
    "description": "Easy loop round the lake",
    "courseName": "Lake loop",
    "workoutName": "Run from 123 Home Street",
    "ownerDisplayName": "jdoe",
    "ownerProfileImageUrlLarge": "https://img.example/1.png",
    "userInfoDto": {"fullname": "Jane Doe", "displayname": "jdoe"},
    "profileImageUrlSmall": "https://img.example/2.png",
}
# Must survive stripping: health data whose names only look similar.
KEPT = {
    "averageOxygenSaturation": 97,  # avera-GEO-xygen
    "timeAllocation": 5,  # al-LOCATION
    "name": "Late Meals",  # lifestyle log entries must survive
    "geography_score": 1,
    "elevationGain": 120.0,  # how hilly, not where
    "maxElevation": 80.0,
    "deviceTypeSimpleName": "Garmin fenix 8",  # a model, not a person
    "longestStreak": 9,
    "latestSpo2": 96,
}

# Shaped like a real get_training_status response; the IDs are made up.
PROFILE_ID, DEVICE_ID, OTHER_DEVICE_ID = 1234567, 3500000001, 3400000002
RECORDED_DEVICES = [
    {"deviceId": DEVICE_ID, "imageURL": "https://res.example/watch.png",
     "deviceName": "fenix 8 - 47mm, AMOLED", "category": 0},
    {"deviceId": OTHER_DEVICE_ID, "imageURL": "https://res.example/old.png",
     "deviceName": "fenix 7X", "category": 0},
]
TRAINING_STATUS = {
    "userId": PROFILE_ID,
    "mostRecentVO2Max": {
        "userId": PROFILE_ID,
        "generic": {"calendarDate": "2026-10-18", "vo2MaxPreciseValue": 51.3, "vo2MaxValue": 51.0},
        "heatAltitudeAcclimation": {
            "calendarDate": "2026-10-19", "altitudeAcclimation": 0,
            "previousAltitudeAcclimation": 0, "heatAcclimationPercentage": 0,
            "currentAltitude": 1650, "previousAltitude": 0,
            "altitudeAcclimationLocalTimestamp": "2026-10-19T23:57:45.0",
        },
    },
    "mostRecentTrainingLoadBalance": {
        "userId": PROFILE_ID,
        "metricsTrainingLoadBalanceDTOMap": {
            str(DEVICE_ID): {
                "calendarDate": "2026-10-19", "deviceId": DEVICE_ID,
                "monthlyLoadAerobicLow": 606.9, "monthlyLoadAnaerobic": 131.0,
                "trainingBalanceFeedbackPhrase": "AEROBIC_HIGH_SHORTAGE",
                "primaryTrainingDevice": True,
            },
        },
        "recordedDevices": RECORDED_DEVICES,
    },
    "mostRecentTrainingStatus": {
        "userId": PROFILE_ID,
        "latestTrainingStatusData": {
            str(DEVICE_ID): {
                "calendarDate": "2026-10-19", "trainingStatus": 8,
                "deviceId": DEVICE_ID, "sport": "RUNNING",
                "trainingStatusFeedbackPhrase": "STRAINED_4",
                "acuteTrainingLoadDTO": {"acwrPercent": 42, "dailyTrainingLoadAcute": 255},
                "primaryTrainingDevice": True,
            },
            str(OTHER_DEVICE_ID): {"calendarDate": "2026-09-01", "trainingStatus": 4,
                                   "deviceId": OTHER_DEVICE_ID},
        },
        "recordedDevices": RECORDED_DEVICES,
        "showSelector": False,
    },
    "heatAltitudeAcclimationDTO": None,
}
# Shaped like a real get_devices entry.
DEVICE = {
    "deviceId": DEVICE_ID, "unitId": DEVICE_ID, "serialNumber": "5ABC12345",
    "displayName": "Bartek's watch", "productDisplayName": "fenix 8",
    "imageUrl": "https://res.example/watch.png",
    "gcmSettingsFile": "Fenix8_dd1ba4bc-485d-437b-b7bb-3e11f1b1526c.json",
    "deviceTypeSimpleName": "Garmin fenix 8", "currentFirmwareVersion": "21.19",
    "userProfileCapable": True,
}


class RecordingApi:
    """Records every endpoint requested and answers with location-laden data."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def endpoint(*args, **kwargs):
            self.calls.append(name)
            return LOCATED
        return endpoint


@pytest.fixture
def api(monkeypatch):
    fake = RecordingApi()
    monkeypatch.setattr(mcp_server, "_api", fake)
    return fake


@pytest.fixture(scope="module")
def remote():
    return mcp_server.build_server(HttpSettings(public_url="https://x.example"))


@pytest.fixture(scope="module", params=["stdio", "http"])
def server(request, remote):
    """Each transport's server: the policy must not depend on it."""
    return mcp_server.mcp if request.param == "stdio" else remote


def call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


# --- the allowlist itself ----------------------------------------------------


def test_allowlisted_names_exist_in_garminconnect():
    # A library rename must not silently drop an endpoint.
    assert endpoints.ALLOWED <= GETTERS


def test_every_getter_is_allowed_or_denied():
    # A getter garminconnect adds must be decided on, not silently blocked
    # (or, worse, silently exposed).
    undecided = GETTERS - endpoints.ALLOWED - set(endpoints.DENIED)
    assert not undecided, f"add these to endpoints.ALLOWED or DENIED: {sorted(undecided)}"


def test_no_getter_is_both_allowed_and_denied():
    assert not endpoints.ALLOWED & set(endpoints.DENIED)


def test_denied_names_exist_in_garminconnect():
    assert set(endpoints.DENIED) <= GETTERS
    assert all(endpoints.DENIED.values())  # each with its reason


@pytest.mark.parametrize("name", SENSITIVE)
def test_sensitive_endpoints_denied(name):
    assert name in endpoints.DENIED


def test_every_single_day_metric_is_decided():
    # get_garmin_day's metrics in particular: the gap that hid new ones.
    for name in endpoints.single_day_metrics():
        assert name in endpoints.ALLOWED or name in endpoints.DENIED, name


def test_default_day_metrics_allowlisted():
    assert set(endpoints.DEFAULT_DAY_METRICS) <= endpoints.ALLOWED


# --- negative: disallowed endpoints, through every entry point ---------------


@pytest.mark.parametrize("name", DISALLOWED)
def test_call_endpoint_rejects_disallowed(api, server, name):
    with pytest.raises(ToolError, match="not available"):
        call(server, "call_garmin_endpoint", {"endpoint": f"get_{name}", "args": {}})
    assert api.calls == []  # rejected before any Garmin request


def test_disallowed_rejection_needs_no_login(monkeypatch, server):
    # Not even a login: rejection happens before the API is touched.
    monkeypatch.setattr(mcp_server, "_api", None)
    monkeypatch.setattr(mcp_server, "login", lambda **_: pytest.fail("logged in"))
    with pytest.raises(ToolError, match="not available"):
        call(server, "call_garmin_endpoint", {"endpoint": "get_user_profile"})


def test_list_endpoints_only_allowlisted(api, server):
    names = {e["endpoint"].removeprefix("get_") for e in call(server, "list_garmin_endpoints", {})}
    assert names == endpoints.ALLOWED
    assert not names & set(DISALLOWED)


@pytest.mark.parametrize("name", DISALLOWED)
def test_day_rejects_disallowed_metric(api, server, name):
    # The schema's enum only lists allowed metrics, so the call is refused
    # before any code runs.
    with pytest.raises(ToolError, match="Input should be"):
        call(server, "get_garmin_day", {"date": "2026-10-05", "metrics": [name, "sleep_data"]})
    assert api.calls == []


@pytest.mark.parametrize("name", DISALLOWED)
def test_day_metrics_guard_disallowed_metric_too(api, name):
    # Underneath the schema, day_metrics refuses them as well.
    out = endpoints.day_metrics(api, "2026-10-05", [name, "sleep_data"], endpoints.ALLOWED)
    assert "not available" in out[name]["error"].lower()
    assert name not in out[name]["error"].split("Available:")[1]
    assert api.calls == ["get_sleep_data"]


def test_no_allowlisted_name_is_a_private_key():
    # get_garmin_day keys results by metric name; stripping must never hide
    # an allowed metric.
    assert not [n for n in endpoints.ALLOWED if endpoints.is_private_key(n)]


def test_unknown_getter_blocked_by_default(api, server, monkeypatch):
    # A getter garminconnect might add later: blocked until allowlisted.
    monkeypatch.setattr(Garmin, "get_new_thing", lambda self: {}, raising=False)
    with pytest.raises(ToolError, match="not available"):
        call(server, "call_garmin_endpoint", {"endpoint": "get_new_thing"})
    assert api.calls == []


# --- positive ----------------------------------------------------------------


def test_allowed_endpoint_works(api, server):
    call(server, "call_garmin_endpoint", {"endpoint": "get_activity", "args": {"activity_id": "1"}})
    assert api.calls == ["get_activity"]


# --- location stripping ------------------------------------------------------


def test_strip_private_nested():
    assert endpoints.strip_private(LOCATED) == {
        "activityId": 1,
        "summaryDTO": {"calories": 500.0},
        "lapDTOs": [{"distance": 1000}],
    }


def test_strip_private_keeps_lookalike_keys():
    assert endpoints.strip_private(KEPT) == KEPT
    assert endpoints.strip_private({"x": [KEPT]}) == {"x": [KEPT]}


def test_training_status_has_no_ids_device_names_or_altitude():
    out = endpoints.strip_private(TRAINING_STATUS)
    text = json.dumps(out)
    for value in (PROFILE_ID, DEVICE_ID, OTHER_DEVICE_ID, "fenix", "res.example"):
        assert str(value) not in text
    for key in ("userId", "deviceId", "deviceName", "imageURL", "Altitude", "altitude"):
        assert key not in text
    # Device-keyed maps keep their data under neutral keys, one per device.
    status = out["mostRecentTrainingStatus"]["latestTrainingStatusData"]
    assert status["device"]["trainingStatusFeedbackPhrase"] == "STRAINED_4"
    assert status["device 2"] == {"calendarDate": "2026-09-01", "trainingStatus": 4}
    balance = out["mostRecentTrainingLoadBalance"]["metricsTrainingLoadBalanceDTOMap"]
    assert balance["device"]["monthlyLoadAerobicLow"] == 606.9
    assert out["mostRecentTrainingStatus"]["recordedDevices"] == [{"category": 0}] * 2
    assert out["mostRecentVO2Max"]["generic"]["vo2MaxValue"] == 51.0
    # Like the denied heat_altitude_acclimation endpoint: it reveals travel.
    assert "heatAltitudeAcclimation" not in out["mostRecentVO2Max"]


def test_devices_keep_the_model_but_nothing_that_identifies_them():
    out = endpoints.strip_private([DEVICE])
    assert out == [{"deviceTypeSimpleName": "Garmin fenix 8", "currentFirmwareVersion": "21.19"}]


# Shaped like a gear-service v2 gear object; `name` and `notes` are the owner's.
GEAR = {
    "uuid": "c5e1a2b3-0000-4000-8000-000000000001", "gearType": "SHOES",
    "brand": "Anta", "model": "A-Flash", "name": "Jane's race shoes",
    "notes": "Kept at 123 Home Street", "maxUsageDistanceMeters": 800000,
}


class GearApi:
    def get_gear_list(self, *args, **kwargs):
        return [GEAR]

    def get_gear_details(self, gear_uuid):
        return GEAR

    def get_activity_gear_v2(self, activity_id):
        return [GEAR]

    def get_activity_gear(self, activity_id):
        return [{"gearPk": 1, "customMakeModel": "Jane's bike", "gearMakeName": "Trek"}]


@pytest.mark.parametrize("endpoint, args", [
    ("get_gear_list", {}), ("get_gear_details", {"gear_uuid": GEAR["uuid"]}),
    ("get_activity_gear_v2", {"activity_id": 1}),
])
def test_gear_nicknames_and_notes_are_dropped(server, monkeypatch, endpoint, args):
    monkeypatch.setattr(mcp_server, "_api", GearApi())
    out = call(server, "call_garmin_endpoint", {"endpoint": endpoint, "args": args})
    gear = out[0] if isinstance(out, list) else out
    assert gear == {
        "uuid": GEAR["uuid"], "gearType": "SHOES", "brand": "Anta", "model": "A-Flash",
        "maxUsageDistanceMeters": 800000,
    }


def test_legacy_gear_nickname_is_dropped(server, monkeypatch):
    monkeypatch.setattr(mcp_server, "_api", GearApi())
    out = call(server, "call_garmin_endpoint", {"endpoint": "get_activity_gear", "args": {"activity_id": 1}})
    assert out == [{"gearPk": 1, "gearMakeName": "Trek"}]


def test_endpoint_private_keys_name_allowed_getters():
    assert {e.removeprefix("get_") for e in endpoints.ENDPOINT_PRIVATE_KEYS} <= endpoints.ALLOWED


def test_queued_workout_titles_are_dropped():
    # get_active_devices lists each device's pending messages too.
    device = {"messageType": "workouts", "messageStatus": "new",
              "messageName": "Hill repeats from 123 Home Street"}
    assert endpoints.strip_private(device) == {"messageType": "workouts", "messageStatus": "new"}


def test_free_text_notes_are_dropped():
    reading = {"systolic": 120, "diastolic": 80, "notes": "after the run home from work"}
    assert endpoints.strip_private(reading) == {"systolic": 120, "diastolic": 80}


def test_numeric_keys_that_are_no_device_id_are_kept():
    # Small numbers (zones, ranks) are never taken for device IDs.
    data = {"zones": {"1": 60, "2": 120}, "deviceId": 2}
    assert endpoints.strip_private(data) == {"zones": {"1": 60, "2": 120}}

    data = {"a": [1, {"b": None, "c": [{"d": "x"}]}], "e": 2.5, "f": "latitude"}
    assert endpoints.strip_private(data) == data
    assert endpoints.strip_private([LOCATED])[0] == endpoints.strip_private(LOCATED)


def test_results_are_stripped(api, server):
    out = call(server, "call_garmin_endpoint", {"endpoint": "get_activity", "args": {"activity_id": "1"}})
    assert out == endpoints.strip_private(LOCATED)


@pytest.mark.parametrize("endpoint, args", [
    ("get_workout_by_id", {"workout_id": 1}), ("get_workouts", {}),
])
def test_workout_names_do_not_expose_places(api, server, endpoint, args):
    out = call(server, "call_garmin_endpoint", {"endpoint": endpoint, "args": args})
    assert api.calls == [endpoint]
    assert out == {"activityId": 1, "summaryDTO": {"calories": 500.0},
                   "lapDTOs": [{"distance": 1000}]}


def test_actual_age_is_stripped_but_fitness_age_kept():
    fitness_age = {
        "chronologicalAge": 41, "fitnessAge": 33.5, "achievableFitnessAge": 31.0,
        "previousFitnessAge": 34.0, "components": {"rhr": {"value": 48}},
        "userData": {"birthDate": "1985-01-01", "dateOfBirth": "1985-01-01", "birthday": "01-01"},
    }
    assert endpoints.strip_private(fitness_age) == {
        "fitnessAge": 33.5, "achievableFitnessAge": 31.0, "previousFitnessAge": 34.0,
        "components": {"rhr": {"value": 48}}, "userData": {},
    }


# Shaped like a real get_fitnessage_data response; 47 is the owner's real age.
REAL_AGE = 47
FITNESS_AGE = {
    "chronologicalAge": REAL_AGE, "fitnessAge": 38.2, "achievableFitnessAge": 35.0,
    "previousFitnessAge": 39.0,
    "components": {"rhr": {"value": 48}, "bmi": {"value": 23.1}, "vigorousDaysAvg": {"value": 3.4}},
    "lastUpdated": "2026-10-11T07:00:00.0",
}


class FitnessAgeApi:
    def get_fitnessage_data(self, cdate):
        return FITNESS_AGE


def has_value(value, wanted) -> bool:
    if isinstance(value, dict):
        return any(has_value(v, wanted) for v in value.values())
    if isinstance(value, list):
        return any(has_value(v, wanted) for v in value)
    return value == wanted


@pytest.mark.parametrize("tool, args", [
    ("call_garmin_endpoint", {"endpoint": "get_fitnessage_data", "args": {"cdate": "2026-10-11"}}),
    ("get_garmin_day", {"date": "2026-10-11", "metrics": ["fitnessage_data"]}),
])
def test_results_never_carry_the_real_age(server, monkeypatch, tool, args):
    monkeypatch.setattr(mcp_server, "_api", FitnessAgeApi())
    out = call(server, tool, args)
    data = out if tool == "call_garmin_endpoint" else out["fitnessage_data"]
    # The real age is gone, wherever it might hide; fitness age stays.
    assert not has_value(out, REAL_AGE)
    assert data["fitnessAge"] == 38.2 and data["components"]["rhr"] == {"value": 48}


def test_day_results_are_stripped(api, server):
    out = call(server, "get_garmin_day", {"date": "d", "metrics": ["sleep_data"]})
    assert out["sleep_data"] == endpoints.strip_private(LOCATED)


# --- one choke point, failing closed -----------------------------------------


def test_default_exposure_is_the_policy():
    # Code running outside a tool wrapper gets the policy too.
    assert mcp_server._exposure.get() is mcp_server.POLICY


@pytest.mark.parametrize("attr", ["get_user_profile", "get_new_thing", "download_activity", "garth"])
def test_garmin_stand_in_blocks_outside_allowlist_by_default(api, attr):
    with pytest.raises(ValueError, match="not available"):
        getattr(mcp_server._Garmin(), attr)
    assert api.calls == []


def test_garmin_stand_in_allows_allowlisted_by_default(api):
    mcp_server._Garmin().get_sleep_data("d")
    assert api.calls == ["get_sleep_data"]


@pytest.mark.parametrize(
    "tool, args",
    [
        ("get_garmin_summary", {"date": "2026-10-05", "metrics": ["weight"]}),
        ("get_garmin_summary", {"date": "2026-10-05", "metrics": ["activity"]}),
    ],
)
def test_curated_tools_go_through_the_stand_in(api, remote, monkeypatch, tool, args):
    # Their Garmin calls are checked against the allowlist too.
    monkeypatch.setattr(endpoints, "ALLOWED", frozenset())
    monkeypatch.setattr(mcp_server, "POLICY", mcp_server._Exposure(frozenset(), endpoints.strip_private))
    blocked = mcp_server.build_server()
    out = call(blocked, tool, args)
    # Reported as that group's error; Garmin is never asked.
    group = args["metrics"][0]
    assert "not available" in out["errors"][group]
    assert out["days"] == [] and api.calls == []


# --- Garmin login failures ------------------------------------------------------


@pytest.fixture
def failing_login(monkeypatch):
    attempts = []

    def login(**_):
        attempts.append(1)
        raise RuntimeError("Garmin login needs an MFA code")

    monkeypatch.setattr(mcp_server, "_api", None)
    monkeypatch.setattr(mcp_server, "login", login)
    return attempts


@pytest.mark.parametrize("server", ["stdio", "remote"])
def test_day_fails_once_when_login_fails(failing_login, remote, server):
    target = mcp_server.mcp if server == "stdio" else remote
    with pytest.raises(ToolError, match="needs an MFA code"):
        call(target, "get_garmin_day", {"date": "2026-10-05"})  # 8 default metrics
    assert failing_login == [1]  # one attempt, not one per metric


def test_call_endpoint_reports_login_failure(failing_login, remote):
    with pytest.raises(ToolError, match="needs an MFA code"):
        call(remote, "call_garmin_endpoint", {"endpoint": "get_sleep_data", "args": {"cdate": "d"}})
    assert failing_login == [1]


# --- the range endpoints get_garmin_summary uses ---------------------------------------


@pytest.mark.parametrize(
    "getter", ["get_hrv_data_range", "get_sleep_daily", "get_training_readiness_range"]
)
def test_range_getters_reachable(api, getter):
    getattr(mcp_server._Garmin(), getter)("2026-10-01", "2026-10-08")
    assert api.calls == [getter]


@pytest.mark.parametrize(
    "path",
    [
        "/userprofile-service/socialProfile",
        "/activity-service/activity/1/details",
        # Reached through their getters, so never as raw paths.
        "/hrv-service/hrv/daily/2026-10-01/2026-10-08",
        "/sleep-service/stats/sleep/daily/2026-10-01/2026-10-08",
        "/metrics-service/metrics/trainingreadiness/2026-10-01/2026-10-08",
        "https://connectapi.garmin.com/userprofile-service/socialProfile",
    ],
)
def test_raw_paths_blocked(api, path):
    with pytest.raises(ValueError, match="not available"):
        mcp_server._Garmin().connectapi(path)
    assert api.calls == []


def test_summary_works_and_strips_nothing_it_needs(api, remote, monkeypatch):
    def get_hrv_data_range(start, end):
        api.calls.append("get_hrv_data_range")
        return {"hrvSummaries": [{"calendarDate": "2026-10-05", "lastNightAvg": 41}]}

    monkeypatch.setattr(api, "get_hrv_data_range", get_hrv_data_range, raising=False)
    out = call(remote, "get_garmin_summary", {"date": "2026-10-05", "metrics": ["hrv"]})
    assert out == {"fields": ["date", "hrv_last_night_ms"], "days": [["2026-10-05", 41]]}


@pytest.mark.parametrize("http", [None, HttpSettings(public_url="https://x.example")])
def test_day_enum_lists_only_allowlisted_metrics(monkeypatch, http):
    monkeypatch.setattr(mcp_server, "POLICY", mcp_server._Exposure(
        frozenset({"sleep_data", "hrv_data"}), endpoints.strip_private))
    server = mcp_server.build_server(http)
    day = next(t for t in asyncio.run(server.list_tools()) if t.name == "get_garmin_day")
    assert day.inputSchema["properties"]["metrics"]["anyOf"][0]["items"]["enum"] == [
        "hrv_data", "sleep_data",
    ]
    assert "- sleep_data:" in day.description and "- stress_data:" not in day.description


def test_long_range_refused_before_any_request(api, remote):
    with pytest.raises(ToolError, match="at most 366"):
        call(remote, "call_garmin_endpoint", {
            "endpoint": "get_sleep_daily", "args": {"start": "1900-01-01", "end": "2026-10-01"},
        })
    assert api.calls == []
