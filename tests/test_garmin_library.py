"""How the real garminconnect client reports Garmin's answers, with only its
HTTP layer faked (no network, no account).

The server tells "Garmin logged you out: sign in again" apart from "Garmin
is down or limiting requests" by the exception garminconnect raises. These
tests pin that down for the installed version: if an upgrade changes it,
they fail instead of the owner getting the wrong instructions.

They replace private parts of garminconnect's client: its API session, its
token-refresh POST and its token fields. If a garminconnect upgrade renames
any of them, the tests must fail, not pass for the wrong reason or reach
Garmin. Three layers make sure of that:

1. stub() refuses to replace an attribute the real client doesn't have, so
   a rename fails before any request is attempted;
2. the network is blocked for requests and curl_cffi (which garminconnect
   uses for token refresh and login), and every proxy variable points at a
   closed local port, so any other HTTP client can't reach Garmin either;
3. every test checks the fake Garmin saw the requests it expects, so a
   result produced some other way doesn't count.
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


def stub(obj, name: str, value) -> None:
    """Replace obj.name, which must already exist: if garminconnect renamed
    it, setting it would only add an attribute the library never reads.
    """
    assert hasattr(obj, name), (
        f"garminconnect's {type(obj).__name__} has no {name!r} any more; "
        "re-check how it makes requests and update these tests"
    )
    setattr(obj, name, value)


def no_network(*args, **kwargs):
    raise AssertionError("tried to reach the network")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No test here may reach Garmin, whatever garminconnect uses."""
    monkeypatch.setattr(requests.Session, "request", no_network)
    try:
        import curl_cffi.requests as cffi
    except ImportError:
        pass
    else:
        monkeypatch.setattr(cffi.Session, "request", no_network)
        for name in ("request", "get", "post"):
            monkeypatch.setattr(cffi, name, no_network)
    # Anything else that honours proxy settings goes to a closed port.
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
        monkeypatch.setenv(var.lower(), "http://127.0.0.1:9")
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def garmin():
    """A real Garmin client holding (fake) tokens."""
    api = Garmin(retry_attempts=0)
    api.display_name = "owner"
    client = api.client
    stub(client, "di_token", "old-access")
    stub(client, "di_refresh_token", "refresh")
    stub(client, "di_client_id", "cid")
    stub(client, "_tokenstore_path", None)  # don't write tokens anywhere
    return api


def refresh_answers(api, status: int, new_token: str = "new-access"):
    attempts = []

    def post(url, **kwargs):
        attempts.append(kwargs.get("data", {}).get("refresh_token"))
        body = {"access_token": new_token, "refresh_token": "new-refresh"} if status == 200 else {}
        r = response(status, body, url)
        r.__dict__["text"] = "invalid_grant" if status != 200 else ""
        return r

    stub(api.client, "_http_post", post)
    return attempts


def test_renewal_that_works_is_used(garmin):
    # Positive control: the stubs really drive garminconnect's own renewal.
    session = FakeGarminApi({"new-access": 200})
    stub(garmin.client, "_api_session", session)
    attempts = refresh_answers(garmin, 200)
    assert garmin.get_sleep_data("2026-10-05")["dailySleepDTO"]["sleepTimeSeconds"] == 25000
    assert attempts == ["refresh"]
    assert session.requests == ["old-access", "new-access"]
    assert garmin.client.di_token == "new-access"


def test_logged_out_is_an_authentication_error(garmin):
    # Garmin rejects the token and the refresh token: logged out.
    session = FakeGarminApi({})
    stub(garmin.client, "_api_session", session)
    attempts = refresh_answers(garmin, 400)
    with pytest.raises(GarminConnectAuthenticationError):
        garmin.get_sleep_data("2026-10-05")
    assert attempts == ["refresh"]  # it tried to renew once
    assert session.requests == ["old-access", "old-access"]  # and retried once


def test_rate_limiting_is_not_an_authentication_error(garmin):
    stub(garmin.client, "_api_session", FakeGarminApi({"old-access": 429}))
    refresh_answers(garmin, 200)
    with pytest.raises(GarminConnectTooManyRequestsError):
        garmin.get_sleep_data("2026-10-05")


def test_refresh_lock_still_wraps_the_real_client(garmin):
    # kcal serializes garminconnect's refresh by wrapping this method.
    _serialize_token_refresh(garmin.client)
    assert garmin.client._refresh_session.kcal_serialized
    session = FakeGarminApi({"new-access": 200})
    stub(garmin.client, "_api_session", session)
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
    session = FakeGarminApi({})
    stub(garmin.client, "_api_session", session)
    attempts = refresh_answers(garmin, 400)
    monkeypatch.setattr(mcp_server, "_api", garmin)
    message = tool_error(server, tool, args)
    assert "Garmin session expired" in message and f"open {PUBLIC}/login" in message
    assert mcp_server._api is None
    # Reached through Garmin's actual answers, not some other failure.
    assert session.requests == ["old-access", "old-access"] and attempts == ["refresh"]


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
    stub(garmin.client, "_api_session", session)
    monkeypatch.setattr(mcp_server, "_api", garmin)
    message = tool_error(server, tool, args)
    assert "try again in a few minutes" in message and "/login" not in message
    assert len(session.requests) == 1  # stopped at the first 429
    assert mcp_server._api is garmin  # the session itself is fine: kept
