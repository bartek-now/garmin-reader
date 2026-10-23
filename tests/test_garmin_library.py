"""How the real garminconnect client reports Garmin's answers, with only its
HTTP layer faked (no network, no account).

Dependency inventory and upgrade checklist: docs/garmin-library-upgrades.md.

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
2. the network is blocked three ways: requests and curl_cffi (which
   garminconnect uses for token refresh and login) are patched; every proxy
   variable points at a closed local port, for any other HTTP client; and
   DNS lookups and socket connections to anything but this machine fail,
   whatever library makes them;
3. every test checks the fake Garmin saw the requests it expects, so a
   result produced some other way doesn't count.
"""

import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

import pytest
import requests
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.fastmcp.exceptions import ToolError

from garmin_reader import endpoints, mcp_server
from garmin_reader.auth import GarminRefreshUnavailable, _guard_token_refresh
from garmin_reader.oauth import GarminReaderOAuthProvider
from garmin_reader.settings import HttpSettings
from signed_in import as_user

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
    # And whatever Python code opens a socket can only reach this machine.
    # (Loopback stays allowed: the event loop talks to itself over a local
    # socket on Windows, and the proxy above is local.)
    real_getaddrinfo, real_connect = socket.getaddrinfo, socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_local(host):
            raise AssertionError(f"tried to look up {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def check(address):
        host = address[0] if isinstance(address, tuple) else address
        if not _is_local(host):
            raise AssertionError(f"tried to connect to {address!r}")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect",
                        lambda self, address: check(address) or real_connect(self, address))
    monkeypatch.setattr(socket.socket, "connect_ex",
                        lambda self, address: check(address) or real_connect_ex(self, address))


def _is_local(host) -> bool:
    host = host.decode() if isinstance(host, bytes) else str(host)
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


# --- the safety net itself -------------------------------------------------------


def test_offline_blocks_dns():
    with pytest.raises(AssertionError, match="look up 'connectapi.garmin.com'"):
        socket.getaddrinfo("connectapi.garmin.com", 443)


def test_offline_blocks_raw_sockets():
    with socket.socket() as s, pytest.raises(AssertionError, match="connect to"):
        s.connect(("8.8.8.8", 443))  # an IP, so no DNS lookup involved
    with pytest.raises(AssertionError):
        socket.create_connection(("connectapi.garmin.com", 443), timeout=1)


def test_offline_sends_unpatched_http_clients_to_the_closed_proxy():
    # urllib isn't patched: it goes to the proxy and is refused there. Had it
    # looked up Garmin's name instead, the DNS block would have raised
    # AssertionError, not URLError.
    with pytest.raises(urllib.error.URLError) as err:
        urllib.request.urlopen("https://connectapi.garmin.com/", timeout=5)
    assert isinstance(err.value.reason, ConnectionRefusedError)


def test_offline_blocks_requests_and_curl_cffi():
    with pytest.raises(AssertionError, match="network"):
        requests.get("https://connectapi.garmin.com/")
    cffi = pytest.importorskip("curl_cffi.requests")
    with pytest.raises(AssertionError, match="network"):
        cffi.post("https://diauth.garmin.com/")


@pytest.fixture
def garmin():
    """A real Garmin client holding (fake) tokens."""
    api = Garmin(retry_attempts=0)
    api.display_name = "owner"
    client = api.client
    # Fail before even the unguarded concurrency checks can deadlock if
    # upstream replaces its reentrant lock with a plain lock.
    assert isinstance(client._token_lock, type(threading.RLock())), (
        "garminconnect's _token_lock must remain a threading.RLock"
    )
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


@pytest.mark.parametrize("guarded", [False, True])
def test_logged_out_is_an_authentication_error(garmin, guarded):
    # Garmin rejects the token and the refresh token: logged out.
    session = FakeGarminApi({})
    stub(garmin.client, "_api_session", session)
    attempts = refresh_answers(garmin, 400)
    if guarded:
        _guard_token_refresh(garmin.client)
    with pytest.raises(GarminConnectAuthenticationError):
        garmin.get_sleep_data("2026-10-05")
    assert attempts == ["refresh"]  # it tried to renew once
    assert session.requests == ["old-access", "old-access"]  # and retried once


def test_rate_limiting_is_not_an_authentication_error(garmin):
    session = FakeGarminApi({"old-access": 429})
    stub(garmin.client, "_api_session", session)
    attempts = refresh_answers(garmin, 200)
    with pytest.raises(GarminConnectTooManyRequestsError):
        garmin.get_sleep_data("2026-10-05")
    assert session.requests == ["old-access"]
    assert attempts == []


def test_refresh_guard_still_wraps_the_real_client(garmin):
    _guard_token_refresh(garmin.client)
    guarded = garmin.client._refresh_session
    _guard_token_refresh(garmin.client)
    assert garmin.client._refresh_session is guarded  # idempotent
    assert garmin.client._refresh_session.garmin_reader_refresh_guarded
    session = FakeGarminApi({"new-access": 200})
    stub(garmin.client, "_api_session", session)
    attempts = refresh_answers(garmin, 200)
    assert garmin.get_sleep_data("2026-10-05")["dailySleepDTO"]["sleepTimeSeconds"] == 25000
    assert session.requests == ["old-access", "new-access"]
    assert attempts == ["refresh"]


@pytest.mark.parametrize("failure", [429, 500, 503, 408, "timeout", "connection"])
def test_upstream_still_hides_temporary_refresh_failures(garmin, failure):
    # Removal check for garmin-reader's HTTP interception. If upstream fixes this,
    # revisit the workaround instead of relaxing this assertion.
    session = FakeGarminApi({})
    stub(garmin.client, "_api_session", session)
    attempts = []

    def post(url, **kwargs):
        attempts.append(kwargs["data"]["refresh_token"])
        if failure == "timeout":
            raise requests.exceptions.Timeout("timed out")
        if failure == "connection":
            raise requests.exceptions.ConnectionError("unreachable")
        return response(failure, {"error": "temporarily_unavailable"}, url)

    stub(garmin.client, "_http_post", post)
    with pytest.raises(GarminConnectAuthenticationError):
        garmin.get_sleep_data("2026-10-05")
    assert attempts == ["refresh"]
    assert session.requests == ["old-access", "old-access"]
    assert garmin.client.di_refresh_token == "refresh"


@pytest.mark.parametrize("guarded", [False, True])
def test_concurrent_real_refresh_rotates_and_persists_tokens(garmin, tmp_path, guarded):
    client = garmin.client
    store = tmp_path / "tokens"
    stub(client, "_tokenstore_path", str(store))
    attempts = []
    active = 0
    peak = 0
    counter_lock = threading.Lock()
    start = threading.Barrier(4)

    def post(url, **kwargs):
        nonlocal active, peak
        with counter_lock:
            active += 1
            peak = max(peak, active)
            attempts.append(kwargs["data"]["refresh_token"])
            number = len(attempts)
        try:
            # Give the other ready workers time to enter an unguarded refresh.
            time.sleep(0.02)
            return response(200, {
                "access_token": f"access-{number}",
                "refresh_token": f"refresh-{number}",
            }, url)
        finally:
            with counter_lock:
                active -= 1

    stub(client, "_http_post", post)
    if guarded:
        _guard_token_refresh(client)

    def refresh():
        start.wait(timeout=5)
        client._refresh_session()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(refresh) for _ in range(4)]
        for future in futures:
            future.result(timeout=10)
    assert peak == 1
    assert attempts == ["refresh", "refresh-1", "refresh-2", "refresh-3"]
    assert client._http_post is post
    assert client.di_token == "access-4"
    assert client.di_refresh_token == "refresh-4"
    restored = Garmin().client
    restored.load(str(store))
    assert restored.di_token == "access-4"
    assert restored.di_refresh_token == "refresh-4"


# --- through the server -----------------------------------------------------------------


USER = "me@example.com"


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The HTTP server, with tool calls made as USER."""
    monkeypatch.setenv("GARMIN_READER_STATE_DIR", str(tmp_path))
    provider = GarminReaderOAuthProvider(tmp_path / "mcp_auth.sqlite", PUBLIC)
    # Profile verification has separate tests; these exercise errors from the
    # health request itself, after an already successful identity check.
    monkeypatch.setattr(mcp_server, "garmin_profile_id", lambda api: 1)
    # USER signed in to Garmin as profile 1; its stored session's revision.
    monkeypatch.setattr(provider, "garmin_session", lambda email: (1, "rev") if email == USER else None)
    server = mcp_server.build_server(
        HttpSettings(public_url=PUBLIC), provider
    )
    with as_user(USER):
        yield server
    provider.close()


def use_session(api) -> None:
    """USER's cached Garmin session is `api`."""
    mcp_server._profile_sessions[1] = mcp_server._ProfileSession(api=api, revision="rev")


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
    use_session(garmin)
    message = tool_error(server, tool, args)
    assert "Garmin session expired" in message and f"open {PUBLIC}/login" in message
    assert mcp_server._profile_sessions[1].api is None
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
    use_session(garmin)
    message = tool_error(server, tool, args)
    assert "try again in a few minutes" in message and "/login" not in message
    assert len(session.requests) == 1  # stopped at the first 429
    assert mcp_server._profile_sessions[1].api is garmin  # the session itself is fine: kept


@pytest.mark.parametrize("failure", [429, 500, 503, 408, "timeout", "connection"])
@pytest.mark.parametrize("tool, args", [
    ("call_garmin_endpoint", {"endpoint": "get_sleep_data", "args": {"cdate": "2026-10-05"}}),
    ("get_garmin_day", {"date": "2026-10-05", "metrics": ["sleep_data", "hrv_data"]}),
    ("get_garmin_summary", {"date": "2026-10-05", "metrics": ["hrv", "sleep"]}),
])
def test_temporary_refresh_failure_keeps_session(server, garmin, monkeypatch, failure, tool, args):
    session = FakeGarminApi({})
    stub(garmin.client, "_api_session", session)
    attempts = []

    def post(url, **kwargs):
        attempts.append(kwargs["data"]["refresh_token"])
        if failure == "timeout":
            raise requests.exceptions.Timeout("timed out")
        if failure == "connection":
            raise requests.exceptions.ConnectionError("unreachable")
        return response(failure, {"error": "temporarily_unavailable"}, url)

    stub(garmin.client, "_http_post", post)
    _guard_token_refresh(garmin.client)
    use_session(garmin)
    # Avoid upstream retries so counts prove no stale-token retry or
    # second metric request occurs after the temporary refresh failure.
    message = tool_error(server, tool, args)
    assert "try again in a few minutes" in message
    assert "/login" not in message and "expired" not in message
    assert mcp_server._profile_sessions[1].api is garmin
    assert session.requests == ["old-access"]
    assert attempts == ["refresh"]
    assert garmin.client._http_post is post
    assert garmin.client.di_token == "old-access"
    assert garmin.client.di_refresh_token == "refresh"

    # Recovery uses the same session and refresh token, without a login.
    session.answers["new-access"] = 200
    recovery = refresh_answers(garmin, 200)
    result = asyncio.run(server.call_tool("call_garmin_endpoint", {
        "endpoint": "get_sleep_data", "args": {"cdate": "2026-10-05"}}))
    assert json.loads(result[0].text)["dailySleepDTO"]["sleepTimeSeconds"] == 25000
    assert recovery == ["refresh"]
    assert session.requests == ["old-access", "old-access", "new-access"]


@pytest.mark.parametrize("failure, exception", [
    (429, GarminConnectTooManyRequestsError), (503, GarminConnectConnectionError),
])
@pytest.mark.parametrize("credentials", [False, True])
def test_cached_login_refresh_failure_never_falls_back_to_credentials(
    garmin, monkeypatch, tmp_path, failure, exception, credentials,
):
    from garmin_reader import auth

    store = tmp_path / "tokens"
    garmin.client.dump(str(store))
    session = FakeGarminApi({})
    stub(garmin.client, "_api_session", session)
    stub(garmin.client, "_token_expires_soon", lambda: True)
    if credentials:
        garmin.username, garmin.password = "owner@example.com", "secret"

    def must_not_login(*args, **kwargs):
        pytest.fail("attempted credential login during a refresh outage")

    stub(garmin.client, "login", must_not_login)
    attempts = []

    def post(url, **kwargs):
        attempts.append(kwargs["data"]["refresh_token"])
        return response(failure, {"error": "temporarily_unavailable"}, url)

    stub(garmin.client, "_http_post", post)
    monkeypatch.setattr(auth, "Garmin", lambda **kwargs: garmin)
    with pytest.raises(exception):
        auth.login(token_store=store)
    assert attempts == ["refresh"]
    assert session.requests == []
    assert garmin.client.login is must_not_login
    assert garmin.client._http_post is post


@pytest.mark.parametrize("failure", [429, 503])
@pytest.mark.parametrize("credentials", [False, True])
def test_upstream_cached_login_still_needs_fallback_guard(
    garmin, tmp_path, failure, credentials,
):
    # Keep the refresh interceptor, omit only auth.login's cache guard.
    # Upstream still catches the outage and either demands credentials or
    # invokes credential login. This check should change when upstream does.
    store = tmp_path / "tokens"
    garmin.client.dump(str(store))
    stub(garmin.client, "_token_expires_soon", lambda: True)
    session = FakeGarminApi({})
    stub(garmin.client, "_api_session", session)
    attempts = refresh_answers(garmin, failure)
    logins = []

    def credential_login(*args, **kwargs):
        logins.append(args)
        raise GarminConnectAuthenticationError("credential fallback reached")

    stub(garmin.client, "login", credential_login)
    if credentials:
        garmin.username, garmin.password = "owner@example.com", "secret"
    _guard_token_refresh(garmin.client)
    with pytest.raises(GarminConnectAuthenticationError) as error:
        garmin.login(str(store))
    assert str(error.value) == (
        "credential fallback reached" if credentials else "Username and password are required"
    )
    assert logins == ([("owner@example.com", "secret")] if credentials else [])
    assert attempts == ["refresh"]
    assert session.requests == []
    assert type(garmin.client.garmin_reader_refresh_error) is (
        GarminConnectTooManyRequestsError if failure == 429 else GarminRefreshUnavailable
    )


# --- the range getters get_garmin_summary relies on --------------------------------


def sleep_score(night: date) -> int:
    """A score unique to each night of the test ranges."""
    return night.timetuple().tm_yday


class FakeRangeApi:
    """Answers Garmin's range endpoints by path, recording each path asked."""

    def __init__(self):
        self.paths = []

    def request(self, method, url, headers=None, **kwargs):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        self.paths.append(path)
        if path.startswith("/sleep-service/stats/sleep/daily/"):
            start, end = (date.fromisoformat(d) for d in path.rsplit("/", 2)[-2:])
            nights = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            # Every night of the chunk, each with its own score, plus the
            # chunk's first night again: merging must de-duplicate.
            body = {"individualStats": [
                {"calendarDate": d.isoformat(), "values": {"sleepScore": sleep_score(d)}}
                for d in nights + nights[:1]
            ]}
            return response(200, body, url)
        if path.startswith("/hrv-service/hrv/daily/"):
            return response(200, {"hrvSummaries": [{"calendarDate": "2026-09-29", "lastNightAvg": 41}]}, url)
        if path.startswith("/metrics-service/metrics/trainingreadiness/"):
            return response(200, [
                {"calendarDate": "2026-09-29", "inputContext": "AFTER_WAKEUP_RESET",
                 "timestamp": "2026-09-29T07:30:00", "score": 61, "level": "MODERATE"},
            ], url)
        return response(404, {}, url)


def test_sleep_daily_chunks_28_days_and_merges(garmin):
    session = FakeRangeApi()
    stub(garmin.client, "_api_session", session)
    nights = garmin.get_sleep_daily("2026-08-01", "2026-09-29")  # 60 days
    assert session.paths == [
        "/sleep-service/stats/sleep/daily/2026-08-01/2026-08-28",
        "/sleep-service/stats/sleep/daily/2026-08-29/2026-09-25",
        "/sleep-service/stats/sleep/daily/2026-09-26/2026-09-29",
    ]
    # Every night of every chunk, once, oldest first, each with its own score.
    expected = [date(2026, 8, 1) + timedelta(days=i) for i in range(60)]
    assert [n["calendarDate"] for n in nights] == [d.isoformat() for d in expected]
    assert [n["values"]["sleepScore"] for n in nights] == [sleep_score(d) for d in expected]


def test_hrv_data_range_is_one_request_returning_summaries(garmin):
    session = FakeRangeApi()
    stub(garmin.client, "_api_session", session)
    out = garmin.get_hrv_data_range("2025-10-01", "2026-09-30")
    assert session.paths == ["/hrv-service/hrv/daily/2025-10-01/2026-09-30"]
    assert out["hrvSummaries"][0]["lastNightAvg"] == 41


def test_training_readiness_range_is_one_request_returning_entries(garmin):
    session = FakeRangeApi()
    stub(garmin.client, "_api_session", session)
    out = garmin.get_training_readiness_range("2025-10-01", "2026-09-30")
    assert session.paths == ["/metrics-service/metrics/trainingreadiness/2025-10-01/2026-09-30"]
    assert out[0]["score"] == 61


def test_summary_reads_the_real_getters_output(garmin):
    from garmin_reader import summary

    stub(garmin.client, "_api_session", FakeRangeApi())
    days = [date(2026, 8, 1) + timedelta(days=i) for i in range(60)]
    out = summary.summarize(garmin, days, ["sleep", "hrv", "readiness"])
    fields, rows = out["fields"], {r[0]: dict(zip(out["fields"], r)) for r in out["days"]}
    assert "errors" not in out
    # Every night, including both sides of each 28-day chunk boundary.
    assert {day: row["sleep_score"] for day, row in rows.items()} == {
        d.isoformat(): sleep_score(d) for d in days
    }
    assert rows["2026-09-29"]["hrv_last_night_ms"] == 41
    assert rows["2026-09-29"]["readiness_score"] == 61
    assert {"sleep_score", "hrv_last_night_ms", "readiness_score"} <= set(fields)


# --- the verification step the login page lets the owner retry --------------------
#
# Only Garmin's MFA verify endpoints are faked: the real _complete_mfa decides
# what each answer means and the real resume_login decides what happens to
# the step, which is what GarminGateway.mfa_pending() then reports.


class FakeSSO:
    """Garmin's MFA verify endpoints: each POST gets the next answer."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.codes = []

    def post(self, url, params=None, headers=None, json=None, data=None, timeout=None):
        # Portal and mobile send the code as JSON, the widget as a form.
        self.codes.append(json["mfaVerificationCode"] if json else data["mfa-code"])
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, requests.Response):
            return answer
        status, body = answer
        return response(status, body, url)


WRONG = (200, {"responseStatus": {"type": "INVALID_MFA_CODE"}})
LIMITED = (429, {})
BLOCKED = (427, {})  # Garmin's non-standard "blocked"
ACCEPTED = (200, {"responseStatus": {"type": "SUCCESSFUL"}, "serviceTicketId": "ST-1"})


def awaiting_code(garmin, answers):
    """The client as a password login that needs a code leaves it (portal
    flow). Those attributes are created by that login, not in __init__, so
    stub() can't check them; if _complete_mfa stops reading them, these
    tests fail instead.
    """
    client = garmin.client
    stub(client, "_mfa_pending", True)
    sso = FakeSSO(answers)
    client._mfa_session, client._mfa_flow, client._mfa_method = sso, "portal", "email"
    client._mfa_login_params, client._mfa_post_headers = {}, {}
    return sso


def test_wrong_mfa_code_leaves_the_step_open(garmin):
    from garmin_reader.login_page import GarminGateway

    sso = awaiting_code(garmin, [WRONG, WRONG])
    with pytest.raises(GarminConnectAuthenticationError):
        garmin.resume_login(None, "000000")
    assert sso.codes == ["000000", "000000"]  # both verify endpoints asked
    assert GarminGateway().mfa_pending(garmin)  # so /login offers the code form again


def test_rate_limited_mfa_code_leaves_the_step_open(garmin):
    from garmin_reader.login_page import GarminGateway

    awaiting_code(garmin, [LIMITED, LIMITED])
    with pytest.raises(GarminConnectTooManyRequestsError):
        garmin.resume_login(None, "123456")
    assert GarminGateway().mfa_pending(garmin)


def page(status: int, text: str) -> requests.Response:
    """A response whose body is `text` as is (HTML, or broken JSON)."""
    r = response(status, None)
    r._content = text.encode()
    return r


TIMEOUT = requests.exceptions.ReadTimeout("Read timed out")
SERVER_ERROR = (503, {"error": {"status-code": "503"}})  # JSON, but no answer
JSON_LIMITED = (200, {"error": {"status-code": "429"}})  # a 200 saying "rate limited"
BOT_CHECK = "bot-check"  # a non-JSON page instead of an answer


def portal_answers(answers):
    return [page(403, "<html>Just a moment...</html>") if a == BOT_CHECK else a for a in answers]


@pytest.mark.parametrize("answers", [
    [TIMEOUT, TIMEOUT],
    [SERVER_ERROR, SERVER_ERROR],
    [BOT_CHECK, BOT_CHECK],
    [SERVER_ERROR, TIMEOUT],
    [LIMITED, TIMEOUT],
    [JSON_LIMITED, TIMEOUT],
    [TIMEOUT, JSON_LIMITED],
    [(200, {"error": {"status-code": "503"}}), BOT_CHECK],
])
def test_portal_code_check_that_got_no_answer_is_unavailable(garmin, answers):
    # garminconnect raises the same authentication error for these as for a
    # wrong code; GarminGateway.finish tells them apart by watching answers.
    from garmin_reader.login_page import GarminGateway, GarminVerificationUnavailable

    sso = awaiting_code(garmin, portal_answers(answers))
    with pytest.raises(GarminVerificationUnavailable):
        GarminGateway().finish(garmin, "123456")
    assert GarminGateway().mfa_pending(garmin)  # the step is still open
    assert garmin.client._mfa_session is sso  # and its session is restored


@pytest.mark.parametrize("answers", [
    [WRONG, TIMEOUT], [TIMEOUT, WRONG], [WRONG, WRONG],
    [WRONG, JSON_LIMITED],  # one real answer is enough
    [(200, {"error": {"status-code": "401"}}), TIMEOUT],  # a 4xx decision is an answer
])
def test_portal_wrong_code_is_still_a_wrong_code(garmin, answers):
    from garmin_reader.login_page import GarminGateway, GarminVerificationUnavailable

    awaiting_code(garmin, answers)
    with pytest.raises(GarminConnectAuthenticationError) as err:
        GarminGateway().finish(garmin, "000000")
    assert not isinstance(err.value, GarminVerificationUnavailable)


@pytest.mark.parametrize(
    "answers",
    [[LIMITED, LIMITED], [JSON_LIMITED, JSON_LIMITED], [LIMITED, JSON_LIMITED], [BLOCKED, BLOCKED], [BLOCKED, LIMITED]],
)
def test_portal_rate_limit_passes_through(garmin, answers):
    from garmin_reader.login_page import GarminGateway

    awaiting_code(garmin, answers)
    with pytest.raises(GarminConnectTooManyRequestsError):
        GarminGateway().finish(garmin, "123456")


def awaiting_widget_code(garmin, answers):
    """The client as a widget-flow login that needs a code leaves it."""
    sso = awaiting_code(garmin, answers)
    garmin.client._mfa_flow = "widget"
    garmin.client._widget_csrf_token = "token-1"  # what the widget's code page carried
    return sso


@pytest.mark.parametrize("answer", [TIMEOUT, (503, {}), (500, {})])
def test_widget_code_check_that_got_no_answer_is_unavailable(garmin, answer):
    from garmin_reader.login_page import GarminGateway, GarminVerificationUnavailable

    awaiting_widget_code(garmin, [answer if answer is TIMEOUT else page(answer[0], "<html>Error</html>")])
    with pytest.raises(GarminVerificationUnavailable):
        GarminGateway().finish(garmin, "123456")
    assert GarminGateway().mfa_pending(garmin)


def test_widget_wrong_code_is_still_a_wrong_code(garmin):
    # The widget answers in HTML: a page that isn't "Success" is an answer.
    from garmin_reader.login_page import GarminGateway, GarminVerificationUnavailable

    awaiting_widget_code(garmin, [page(200, "<html><title>Enter MFA code</title></html>")])
    with pytest.raises(GarminConnectAuthenticationError) as err:
        GarminGateway().finish(garmin, "000000")
    assert not isinstance(err.value, GarminVerificationUnavailable)


@pytest.mark.parametrize("status", [429, 427])
def test_widget_rate_limit_passes_through(garmin, status):
    from garmin_reader.login_page import GarminGateway

    awaiting_widget_code(garmin, [page(status, "")])
    with pytest.raises(GarminConnectTooManyRequestsError):
        GarminGateway().finish(garmin, "123456")


def test_corrected_code_signs_in_and_closes_the_step(garmin):
    from garmin_reader.login_page import GarminGateway

    sso = awaiting_code(garmin, [WRONG, WRONG, ACCEPTED])
    with pytest.raises(GarminConnectAuthenticationError):
        garmin.resume_login(None, "000000")
    tickets = []
    # What follows an accepted code reaches Garmin's servers: faked too.
    stub(garmin.client, "_establish_session",
         lambda ticket, sess=None, service_url=None: tickets.append(ticket))
    stub(garmin.client, "_verify_token", lambda: True)
    stub(garmin, "_load_profile_and_settings", lambda: None)
    garmin.resume_login(None, "123456")
    assert tickets == ["ST-1"] and sso.codes[-1] == "123456"
    assert not GarminGateway().mfa_pending(garmin)


# --- what a missing end date means (endpoints.MISSING_END_IS_START) ----------------


class RecordingApi:
    """Records each API request's path and query; answers with nothing."""

    def __init__(self):
        self.requests = []

    def request(self, method, url, headers=None, params=None, **kwargs):
        self.requests.append((url, dict(params or {})))
        return response(200, {}, url)


@pytest.mark.parametrize("getter", sorted(endpoints.MISSING_END_IS_START))
def test_missing_end_means_the_start_day(garmin, getter):
    session = RecordingApi()
    stub(garmin.client, "_api_session", session)
    args = {"startdate": "2020-01-01"}
    if getter == "get_device_solar_data":
        args["device_id"] = "1"
    # Some getters complain about the empty answer; only the request matters.
    with contextlib.suppress(GarminConnectConnectionError):
        getattr(garmin, getter)(**args)
    # One request, about 2020-01-01 only: never extended to today.
    assert len(session.requests) == 1
    sent = json.dumps(session.requests)
    assert "2020-01-01" in sent and date.today().isoformat() not in sent


def test_missing_end_means_today_for_activities_by_date(garmin):
    # Why the span check assumes "today" when a getter isn't listed: here a
    # missing end leaves the range open, and Garmin pages through it.
    session = RecordingApi()
    stub(garmin.client, "_api_session", session)
    garmin.get_activities_by_date("2020-01-01")
    url, params = session.requests[0]
    assert params.get("startDate") == "2020-01-01" and "endDate" not in params
