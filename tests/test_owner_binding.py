import asyncio
import json
from types import SimpleNamespace

import pytest
from garminconnect import GarminConnectConnectionError
from mcp.server.fastmcp.exceptions import ToolError

from kcal import mcp_server
from kcal.oauth import KcalOAuthProvider
from kcal.settings import HttpSettings

OWNER = 123
PUBLIC = "https://example.com"


class Session:
    def __init__(self, owner):
        self.owner = owner
        self.profiles = []
        self.health = []
        self.client = SimpleNamespace(connectapi=self.profile)

    def profile(self, path):
        self.profiles.append(path)
        assert path == "/userprofile-service/socialProfile"
        return {"profileId": self.owner}

    def get_sleep_data(self, cdate):
        self.health.append(cdate)
        return {"sleepTimeSeconds": 25000}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("KCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_api", None)
    monkeypatch.setattr(mcp_server, "_verified_api", None)
    provider = KcalOAuthProvider(tmp_path / "auth.sqlite", PUBLIC)
    yield mcp_server.build_server(HttpSettings(public_url=PUBLIC, garmin_owner=OWNER), provider)
    provider.close()


def call(server):
    return asyncio.run(server.call_tool("call_garmin_endpoint", {
        "endpoint": "get_sleep_data", "args": {"cdate": "2026-10-05"}}))


@pytest.mark.parametrize("cached", [False, True])
def test_wrong_account_is_rejected_before_health_data(server, monkeypatch, cached):
    api = Session(OWNER + 1)
    monkeypatch.setattr(mcp_server, "login", lambda **kwargs: api)
    if cached:
        monkeypatch.setattr(mcp_server, "_api", api)
    with pytest.raises(ToolError, match="different account") as error:
        call(server)
    assert PUBLIC + "/login" in str(error.value)
    assert api.profiles == ["/userprofile-service/socialProfile"]
    assert api.health == []
    assert mcp_server._api is None
    assert mcp_server._verified_api is None


def test_owner_verified_once_per_session_and_again_after_reset(server, monkeypatch):
    api = Session(OWNER)
    logins = []

    def login(**kwargs):
        logins.append(kwargs)
        return api

    monkeypatch.setattr(mcp_server, "login", login)
    for _ in range(2):
        assert json.loads(call(server)[0].text) == {"sleepTimeSeconds": 25000}
    assert len(logins) == 1
    assert api.profiles == ["/userprofile-service/socialProfile"]
    assert api.health == ["2026-10-05"] * 2
    mcp_server._reset_api()
    api = Session(OWNER + 1)
    with pytest.raises(ToolError, match="different account"):
        call(server)
    assert len(logins) == 2
    assert api.health == []
    assert api.profiles == ["/userprofile-service/socialProfile"]


def test_profile_outage_does_not_verify_or_discard_the_session(server, monkeypatch):
    api = Session(OWNER)
    monkeypatch.setattr(mcp_server, "_api", api)

    def unavailable(path):
        raise GarminConnectConnectionError("temporarily unavailable")

    api.client.connectapi = unavailable
    with pytest.raises(ToolError, match="try again in a few minutes") as error:
        call(server)
    assert "/login" not in str(error.value)
    assert mcp_server._api is api
    assert mcp_server._verified_api is None
    assert api.health == []
    api.client.connectapi = api.profile
    assert json.loads(call(server)[0].text) == {"sleepTimeSeconds": 25000}
    assert api.profiles == ["/userprofile-service/socialProfile"]
