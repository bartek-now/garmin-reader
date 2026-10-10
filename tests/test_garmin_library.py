"""How the real garminconnect client reports Garmin's answers, with only its
HTTP layer faked (no network, no account).

The server tells "Garmin logged you out: sign in again" apart from "Garmin
is down or limiting requests" by the exception garminconnect raises. These
tests pin that down for the installed version: if an upgrade changes it,
they fail instead of the owner getting the wrong instructions.

They replace two private parts of garminconnect's client (its API session
and its token-refresh POST); a rename there makes them fail loudly too.
"""

import asyncio
import json

import pytest
import requests
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.fastmcp.exceptions import ToolError

from kcal import mcp_server
from kcal.auth import _serialize_token_refresh
from kcal.oauth import KcalOAuthProvider
from kcal.settings import HttpSettings

PUBLIC = "https://abc-def.trycloudflare.com"


def response(status: int, body: dict | None = None, url: str = "https://example") -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body or {}).encode()
    r.url = url
    r.reason = {200: "OK", 401: "Unauthorized", 429: "Too Many Requests"}.get(status, "")
    return r


class FakeGarminApi:
    """Stands in for the API session: answers by the bearer token sent."""

    def __init__(self, answers: dict[str, int]):
        self.answers = answers  # token -> status; anything else gets 401
        self.requests = []

    def request(self, method, url, headers=None, **kwargs):
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        self.requests.append(token)
        status = self.answers.get(token, 401)
        body = {"dailySleepDTO": {"sleepTimeSeconds": 25000}} if status == 200 else {}
        return response(status, body, url)


@pytest.fixture
def garmin(monkeypatch):
    """A real Garmin client holding tokens, with no way onto the network."""
    def no_network(*args, **kwargs):
        raise AssertionError("tried to reach the network")

    monkeypatch.setattr(requests.Session, "request", no_network)
    api = Garmin(retry_attempts=0)
    api.display_name = "owner"
    client = api.client
    client.di_token, client.di_refresh_token, client.di_client_id = "old-access", "refresh", "cid"
    client._tokenstore_path = None  # don't write tokens anywhere
    return api


def refresh_answers(api, status: int, new_token: str = "new-access"):
    attempts = []

    def post(url, **kwargs):
        attempts.append(kwargs.get("data", {}).get("refresh_token"))
        body = {"access_token": new_token, "refresh_token": "new-refresh"} if status == 200 else {}
        r = response(status, body, url)
        r.__dict__["text"] = "invalid_grant" if status != 200 else ""
        return r

    api.client._http_post = post
    return attempts


def test_renewal_that_works_is_used(garmin):
    # Positive control: the stubs really drive garminconnect's own renewal.
    session = FakeGarminApi({"new-access": 200})
    garmin.client._api_session = session
    attempts = refresh_answers(garmin, 200)
    assert garmin.get_sleep_data("2026-10-05")["dailySleepDTO"]["sleepTimeSeconds"] == 25000
    assert attempts == ["refresh"]
    assert session.requests == ["old-access", "new-access"]
    assert garmin.client.di_token == "new-access"


def test_logged_out_is_an_authentication_error(garmin):
    # Garmin rejects the token and the refresh token: logged out.
    session = FakeGarminApi({})
    garmin.client._api_session = session
    attempts = refresh_answers(garmin, 400)
    with pytest.raises(GarminConnectAuthenticationError):
        garmin.get_sleep_data("2026-10-05")
    assert attempts == ["refresh"]  # it tried to renew once
    assert session.requests == ["old-access", "old-access"]  # and retried once


def test_rate_limiting_is_not_an_authentication_error(garmin):
    garmin.client._api_session = FakeGarminApi({"old-access": 429})
    refresh_answers(garmin, 200)
    with pytest.raises(GarminConnectTooManyRequestsError):
        garmin.get_sleep_data("2026-10-05")


def test_refresh_lock_still_wraps_the_real_client(garmin):
    # kcal serializes garminconnect's refresh by wrapping this method.
    _serialize_token_refresh(garmin.client)
    assert garmin.client._refresh_session.kcal_serialized
    session = FakeGarminApi({"new-access": 200})
    garmin.client._api_session = session
    refresh_answers(garmin, 200)
    garmin.get_sleep_data("2026-10-05")
    assert session.requests == ["old-access", "new-access"]


# --- through the server -----------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("KCAL_STATE_DIR", str(tmp_path))
    provider = KcalOAuthProvider(tmp_path / "mcp_auth.sqlite", PUBLIC)
    yield mcp_server.build_server(HttpSettings(public_url=PUBLIC, garmin_owner=1), provider)
    provider.close()


def tool_error(server, name, args) -> str:
    with pytest.raises(ToolError) as err:
        asyncio.run(server.call_tool(name, args))
    return str(err.value)


@pytest.mark.parametrize(
    "tool, args",
    [
        ("get_garmin_day", {"date": "2026-10-05", "metrics": ["sleep_data"]}),
        ("call_garmin_endpoint", {"endpoint": "get_sleep_data", "args": {"cdate": "2026-10-05"}}),
    ],
)
def test_real_logout_gets_sign_in_instructions(server, garmin, monkeypatch, tool, args):
    garmin.client._api_session = FakeGarminApi({})
    refresh_answers(garmin, 400)
    monkeypatch.setattr(mcp_server, "_api", garmin)
    message = tool_error(server, tool, args)
    assert "Garmin session expired" in message and f"open {PUBLIC}/login" in message
    assert mcp_server._api is None


@pytest.mark.parametrize(
    "tool, args",
    [
        ("call_garmin_endpoint", {"endpoint": "get_sleep_data", "args": {"cdate": "2026-10-05"}}),
        # Fails the whole call once rather than per metric or per group.
        ("get_garmin_day", {"date": "2026-10-05", "metrics": ["sleep_data", "hrv_data"]}),
        ("get_garmin_summary", {"date": "2026-10-05", "metrics": ["hrv", "sleep"]}),
    ],
)
def test_real_rate_limit_gets_try_again_later(server, garmin, monkeypatch, tool, args):
    session = FakeGarminApi({"old-access": 429})
    garmin.client._api_session = session
    monkeypatch.setattr(mcp_server, "_api", garmin)
    message = tool_error(server, tool, args)
    assert "try again in a few minutes" in message and "/login" not in message
    assert len(session.requests) == 1  # stopped at the first 429
    assert mcp_server._api is garmin  # the session itself is fine: kept
