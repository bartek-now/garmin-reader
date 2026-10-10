import asyncio
import json
import threading

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from kcal import mcp_server


class FakeApi:
    def get_sleep_data(self, cdate):
        return {"date": cdate, "movement": list(range(100))}


@pytest.fixture(autouse=True)
def fake_api(monkeypatch):
    monkeypatch.setattr(mcp_server, "_api", FakeApi())


def call(name, args):
    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    assert len(content) == 1  # one block, not one per list item
    return json.loads(content[0].text)


def test_tools_registered():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert {t.name for t in tools} == {
        "get_garmin_summary",
        "get_garmin_day",
        "list_garmin_endpoints",
        "call_garmin_endpoint",
        "get_kcal_server_info",
    }


def test_get_garmin_day_through_mcp():
    out = call("get_garmin_day", {"date": "2026-10-05", "metrics": ["sleep_data"]})
    assert out["sleep_data"]["date"] == "2026-10-05"


def test_call_garmin_endpoint_through_mcp():
    out = call("call_garmin_endpoint", {"endpoint": "get_sleep_data", "args": {"cdate": "d"}})
    assert out["date"] == "d"


def test_server_info_through_mcp():
    out = call("get_kcal_server_info", {})
    assert out["stale"] is False


def test_summary_errors_reach_caller():
    with pytest.raises(ToolError, match="end date needs a start date"):
        call("get_garmin_summary", {"to_date": "2026-10-05"})


def test_summary_range_limited_by_per_day_groups():
    with pytest.raises(ToolError, match="with activity .* at most 120"):
        call("get_garmin_summary", {"from_date": "2026-01-01", "to_date": "2026-06-30"})


def test_summary_range_limited_to_a_year():
    with pytest.raises(ToolError, match="at most 366"):
        call("get_garmin_summary",
             {"from_date": "2024-01-01", "to_date": "2025-12-31", "metrics": ["hrv"]})


def tool(name, server=None):
    tools = asyncio.run((server or mcp_server.mcp).list_tools())
    return next(t for t in tools if t.name == name)


def test_summary_metrics_are_an_enum():
    schema = tool("get_garmin_summary").inputSchema["properties"]["metrics"]
    enum = schema["anyOf"][0]["items"]["enum"]
    assert enum == list(mcp_server.summary.GROUP_FIELDS)


def test_summary_rejects_unknown_group_before_running():
    with pytest.raises(ToolError, match="hrv"):  # the error lists the valid ones
        call("get_garmin_summary", {"metrics": ["vibes"]})


def test_summary_description_documents_every_group():
    description = tool("get_garmin_summary").description
    for group, doc in mcp_server.summary.GROUP_DOCS.items():
        assert f"- {group}: {doc}" in description


def test_summary_through_mcp(monkeypatch):
    class Api:
        def connectapi(self, path):
            return {"hrvSummaries": [{"calendarDate": "2026-10-05", "lastNightAvg": 41}]}

    monkeypatch.setattr(mcp_server, "_api", Api())
    out = call("get_garmin_summary", {"date": "2026-10-05", "metrics": ["hrv"]})
    assert out == {"fields": ["date", "hrv_last_night_ms"], "days": [["2026-10-05", 41]]}


def test_day_metrics_are_an_enum_of_single_day_endpoints():
    schema = tool("get_garmin_day").inputSchema["properties"]["metrics"]
    assert schema["anyOf"][0]["items"]["enum"] == mcp_server.endpoints.single_day_metrics()


def test_day_rejects_unknown_metric_before_running():
    with pytest.raises(ToolError, match="sleep_data"):
        call("get_garmin_day", {"date": "2026-10-05", "metrics": ["vibes"]})


def test_day_description_documents_every_metric():
    description = tool("get_garmin_day").description
    for name in mcp_server.endpoints.single_day_metrics():
        assert f"- {name}: " in description
    assert "YYYY-MM-DD'" not in description  # garminconnect's boilerplate removed
    assert "{metrics}" not in description


def test_results_are_one_compact_block_without_structured_copy():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert all(t.outputSchema is None for t in tools)
    content = asyncio.run(mcp_server.mcp.call_tool("list_garmin_endpoints", {}))
    assert len(content) == 1
    text = content[0].text
    assert "\n" not in text and '", "' not in text


def test_oversized_list_reports_item_count(monkeypatch):
    monkeypatch.setattr(
        mcp_server.endpoints, "call_endpoint", lambda api, e, a, allowed: ["x" * 1000] * 100
    )
    out = call("call_garmin_endpoint", {"endpoint": "get_sleep_data"})
    assert out["items"] == 100 and "50%" in out["error"]


def test_tools_run_off_the_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen = []
    monkeypatch.setattr(
        mcp_server.endpoints, "call_endpoint",
        lambda api, e, a, allowed: seen.append(threading.get_ident()) or {},
    )
    call("call_garmin_endpoint", {"endpoint": "get_sleep_data"})
    assert seen and seen[0] != loop_thread


def test_day_description_is_dedented_on_every_python(monkeypatch):
    # Before 3.13 docstrings keep their indentation; simulate that, and the
    # metric list must still line up.
    first, *rest = mcp_server.get_garmin_day.__doc__.splitlines()
    indented = "\n".join([first, *("    " + line if line else line for line in rest)])
    monkeypatch.setattr(mcp_server.get_garmin_day, "__doc__", indented)
    lines = tool("get_garmin_day", mcp_server.build_server()).description.splitlines()
    metric_lines = [l for l in lines if l.lstrip().startswith("- ")]
    assert metric_lines and all(l.startswith("- ") for l in metric_lines)
    assert not any(l.startswith(" ") for l in lines)
