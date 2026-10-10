"""What the HTTP server's tools may reach (docs/http-server-design.md, section 5)."""

import asyncio
import json

import pytest
from garminconnect import Garmin
from mcp.server.fastmcp.exceptions import ToolError

from kcal import endpoints, mcp_server
from kcal.settings import HttpSettings

GETTERS = {name.removeprefix("get_") for name in endpoints._getters()}
DISALLOWED = sorted(GETTERS - endpoints.REMOTE_ALLOWED)

# Named in the design doc's "Not allowed" list.
SENSITIVE = [
    "activity_details", "activity_weather", "user_profile",
    "userprofile_settings", "full_name", "unit_system", "devices", "gear",
    "goals", "training_plans", "menstrual_data_for_date",
    "menstrual_calendar_data", "pregnancy_summary",
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
    "ownerDisplayName": "jdoe",
    "ownerProfileImageUrlLarge": "https://img.example/1.png",
    "userInfoDto": {"fullname": "Jane Doe", "displayname": "jdoe"},
    "profileImageUrlSmall": "https://img.example/2.png",
}
# Must survive stripping: health data whose names only look similar.
KEPT = {
    "averageOxygenSaturation": 97,  # avera-GEO-xygen
    "timeAllocation": 5,  # al-LOCATION
    "userProfilePK": 123,
    "activityName": "Morning Run",
    "imageURL": "https://img.example/device.png",
    "geography_score": 1,
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


def call(server, name, args):
    result = asyncio.run(server.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


# --- the allowlist itself ----------------------------------------------------


def test_allowlisted_names_exist_in_garminconnect():
    # A library rename must not silently drop an endpoint.
    assert endpoints.REMOTE_ALLOWED <= GETTERS


@pytest.mark.parametrize("name", SENSITIVE)
def test_sensitive_endpoints_not_allowlisted(name):
    assert name in GETTERS
    assert name not in endpoints.REMOTE_ALLOWED


def test_default_day_metrics_allowlisted():
    assert set(endpoints.DEFAULT_DAY_METRICS) <= endpoints.REMOTE_ALLOWED


# --- negative: disallowed endpoints, through every entry point ---------------


@pytest.mark.parametrize("name", DISALLOWED)
def test_call_endpoint_rejects_disallowed_remotely(api, remote, name):
    with pytest.raises(ToolError, match="not available remotely"):
        call(remote, "call_garmin_endpoint", {"endpoint": f"get_{name}", "args": {}})
    assert api.calls == []  # rejected before any Garmin request


def test_disallowed_rejection_needs_no_login(monkeypatch, remote):
    # Not even a login: rejection happens before the API is touched.
    monkeypatch.setattr(mcp_server, "_api", None)
    monkeypatch.setattr(mcp_server, "login", lambda **_: pytest.fail("logged in"))
    with pytest.raises(ToolError, match="not available remotely"):
        call(remote, "call_garmin_endpoint", {"endpoint": "get_user_profile"})


def test_list_endpoints_remotely_only_allowlisted(api, remote):
    names = {e["endpoint"].removeprefix("get_") for e in call(remote, "list_garmin_endpoints", {})}
    assert names == endpoints.REMOTE_ALLOWED
    assert not names & set(DISALLOWED)


@pytest.mark.parametrize("name", DISALLOWED)
def test_day_rejects_disallowed_metric_remotely(api, remote, name):
    # The schema's enum only lists allowed metrics, so the call is refused
    # before any code runs.
    with pytest.raises(ToolError, match="Input should be"):
        call(remote, "get_garmin_day", {"date": "2026-10-05", "metrics": [name, "sleep_data"]})
    assert api.calls == []


@pytest.mark.parametrize("name", DISALLOWED)
def test_day_metrics_guard_disallowed_metric_too(api, name):
    # Underneath the schema, day_metrics refuses them as well.
    out = endpoints.day_metrics(api, "2026-10-05", [name, "sleep_data"], endpoints.REMOTE_ALLOWED)
    assert "not available remotely" in out[name]["error"].lower()
    assert name not in out[name]["error"].split("Available:")[1]
    assert api.calls == ["get_sleep_data"]


def test_no_allowlisted_name_is_a_private_key():
    # get_garmin_day keys results by metric name; stripping must never hide
    # an allowed metric.
    assert not [n for n in endpoints.REMOTE_ALLOWED if endpoints.is_private_key(n)]


def test_unknown_getter_blocked_remotely_by_default(api, remote, monkeypatch):
    # A getter garminconnect might add later: reachable locally, not remotely.
    monkeypatch.setattr(Garmin, "get_new_thing", lambda self: {}, raising=False)
    with pytest.raises(ToolError, match="not available remotely"):
        call(remote, "call_garmin_endpoint", {"endpoint": "get_new_thing"})
    assert api.calls == []
    call(mcp_server.mcp, "call_garmin_endpoint", {"endpoint": "get_new_thing"})
    assert api.calls == ["get_new_thing"]


# --- positive ----------------------------------------------------------------


def test_allowed_endpoint_works_remotely(api, remote):
    call(remote, "call_garmin_endpoint", {"endpoint": "get_activity", "args": {"activity_id": "1"}})
    assert api.calls == ["get_activity"]


@pytest.mark.parametrize("name", ["user_profile", "devices", "activity_details"])
def test_disallowed_endpoints_still_work_over_stdio(api, name):
    args = {"activity_id": "1"} if name == "activity_details" else {}
    out = call(mcp_server.mcp, "call_garmin_endpoint", {"endpoint": f"get_{name}", "args": args})
    assert api.calls == [f"get_{name}"]
    assert out["summaryDTO"]["startLatitude"] == 52.1  # nothing stripped locally


def test_list_endpoints_over_stdio_lists_everything(api):
    names = {e["endpoint"].removeprefix("get_") for e in call(mcp_server.mcp, "list_garmin_endpoints", {})}
    assert names == GETTERS


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


def test_strip_private_leaves_other_data_alone():
    data = {"a": [1, {"b": None, "c": [{"d": "x"}]}], "e": 2.5, "f": "latitude"}
    assert endpoints.strip_private(data) == data
    assert endpoints.strip_private([LOCATED])[0] == endpoints.strip_private(LOCATED)


def test_remote_results_are_stripped(api, remote):
    out = call(remote, "call_garmin_endpoint", {"endpoint": "get_activity", "args": {"activity_id": "1"}})
    assert out == endpoints.strip_private(LOCATED)


def test_remote_day_results_are_stripped(api, remote):
    out = call(remote, "get_garmin_day", {"date": "d", "metrics": ["sleep_data"]})
    assert out["sleep_data"] == endpoints.strip_private(LOCATED)


# --- one choke point, failing closed -----------------------------------------


def test_default_exposure_is_remote():
    # Code running outside a tool wrapper gets the remote rules.
    assert mcp_server._exposure.get() is mcp_server.REMOTE


@pytest.mark.parametrize("attr", ["get_user_profile", "get_new_thing", "download_activity", "garth"])
def test_garmin_stand_in_blocks_outside_allowlist_by_default(api, attr):
    with pytest.raises(ValueError, match="not available remotely"):
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
    monkeypatch.setattr(endpoints, "REMOTE_ALLOWED", frozenset())
    monkeypatch.setattr(mcp_server, "REMOTE", mcp_server._Exposure(frozenset(), endpoints.strip_private))
    blocked = mcp_server.build_server(HttpSettings(public_url="https://x.example"))
    out = call(blocked, tool, args)
    # Reported as that group's error; Garmin is never asked.
    group = args["metrics"][0]
    assert "not available remotely" in out["errors"][group]
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
    "path",
    [
        "/hrv-service/hrv/daily/2026-10-01/2026-10-08",
        "/sleep-service/stats/sleep/daily/2026-10-01/2026-10-08",
        "/metrics-service/metrics/trainingreadiness/2026-10-01/2026-10-08",
    ],
)
def test_range_endpoints_reachable_remotely(api, path):
    mcp_server._Garmin().connectapi(path)  # default exposure is the remote one
    assert api.calls == ["connectapi"]


@pytest.mark.parametrize(
    "path",
    [
        "/userprofile-service/socialProfile",
        "/activity-service/activity/1/details",
        "/hrv-service/hrv/daily/2026-10-01/2026-10-08/../../../userprofile-service/socialProfile",
        "/hrv-service/hrv/daily/2026-10-01/2026-10-08?x=1",
    ],
)
def test_other_raw_paths_blocked_remotely(api, path):
    with pytest.raises(ValueError, match="not available remotely"):
        mcp_server._Garmin().connectapi(path)
    assert api.calls == []


def test_raw_path_extra_arguments_blocked_remotely(api):
    with pytest.raises(ValueError, match="not available remotely"):
        mcp_server._Garmin().connectapi("/hrv-service/hrv/daily/2026-10-01/2026-10-08", method="POST")


def test_summary_works_remotely_and_strips_nothing_it_needs(api, remote, monkeypatch):
    def connectapi(path):
        api.calls.append("connectapi")
        return {"hrvSummaries": [{"calendarDate": "2026-10-05", "lastNightAvg": 41}]}

    monkeypatch.setattr(api, "connectapi", connectapi, raising=False)
    out = call(remote, "get_garmin_summary", {"date": "2026-10-05", "metrics": ["hrv"]})
    assert out == {"fields": ["date", "hrv_last_night_ms"], "days": [["2026-10-05", 41]]}


def test_remote_day_enum_lists_only_allowlisted_metrics(remote, monkeypatch):
    import asyncio
    monkeypatch.setattr(mcp_server, "REMOTE", mcp_server._Exposure(
        frozenset({"sleep_data", "hrv_data"}), endpoints.strip_private))
    server = mcp_server.build_server(HttpSettings(public_url="https://x.example"))
    day = next(t for t in asyncio.run(server.list_tools()) if t.name == "get_garmin_day")
    assert day.inputSchema["properties"]["metrics"]["anyOf"][0]["items"]["enum"] == [
        "hrv_data", "sleep_data",
    ]
    assert "- sleep_data:" in day.description and "- stress_data:" not in day.description
