"""The /login page (design sections 2 and 3), with a fake Garmin."""

import asyncio
import json
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from kcal import cli, login_page, mcp_server
from kcal.oauth import KcalOAuthProvider
from kcal.settings import HttpSettings

PUBLIC = "https://abc-def.trycloudflare.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
OWNER = 12345
EMAIL, PASSWORD = "owner@example.com", "hunter2-secret"


class FakeGarmin:
    """Accounts: email -> (password, profile_id, mfa_code or None)."""

    accounts = {
        EMAIL: (PASSWORD, OWNER, None),
        "mfa@example.com": (PASSWORD, OWNER, "123456"),
        "other@example.com": (PASSWORD, 999, None),
    }

    def __init__(self):
        self.calls = []
        self.error = None  # raised by start() if set
        # Like garminconnect 0.3.17, a wrong code leaves the step open;
        # set to True to model Garmin closing it instead.
        self.wrong_code_closes_step = False
        self.finish_failures = []  # raised (in order) before a code is checked

    def start(self, email, password):
        self.calls.append(("start", email))
        if self.error:
            raise self.error
        account = self.accounts.get(email)
        if account is None or account[0] != password:
            raise GarminConnectAuthenticationError("401 Unauthorized")
        session = {"email": email, "profile": account[1], "mfa": account[2],
                   "done": not account[2], "mfa_open": account[2] is not None}
        return session, account[2] is not None

    def finish(self, session, code):
        self.calls.append(("finish", code))
        if self.finish_failures:
            raise self.finish_failures.pop(0)
        if code != session["mfa"]:
            if self.wrong_code_closes_step:
                session["mfa_open"] = False
            raise GarminConnectAuthenticationError("bad MFA code")
        session["done"], session["mfa_open"] = True, False

    def mfa_pending(self, session):
        return session["mfa_open"]

    def profile_id(self, session):
        assert session["done"]
        return session["profile"]

    def save(self, session, token_store):
        token_store.mkdir(parents=True, exist_ok=True)
        (token_store / "saved.json").write_text(json.dumps(session))


@pytest.fixture
def garmin(monkeypatch):
    fake = FakeGarmin()
    monkeypatch.setattr(login_page, "GarminGateway", lambda: fake)
    return fake


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("KCAL_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def provider(state):
    p = KcalOAuthProvider(state / "mcp_auth.sqlite", PUBLIC)
    yield p
    p.close()


@pytest.fixture
def server(garmin, provider):
    return mcp_server.build_server(HttpSettings(public_url=PUBLIC, garmin_owner=OWNER), provider)


@pytest.fixture
def client(server):
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        yield c


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def register(client, name="Claude"):
    r = client.post("/register", json={
        "client_name": name, "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none",
    })
    return r.json()["client_id"]


def start_authorization(client, name="Claude"):
    client_id = register(client, name)
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256", "state": "st4te",
    }, follow_redirects=False)
    return client_id, query(r.headers["location"])["req"]


def open_form(client, req=None):
    r = client.get("/login", params={"req": req} if req else {})
    return r, client.cookies.get(login_page.CSRF_COOKIE)


def sign_in(client, req=None, email=EMAIL, password=PASSWORD, csrf=None):
    if csrf is None:
        _, csrf = open_form(client, req)
    return client.post("/login", data={
        "csrf": csrf, "req": req or "", "email": email, "password": password, "action": "signin",
    }, follow_redirects=False)


def flow_id(body):
    return body.split('name="flow" value="')[1].split('"')[0]


# --- the form ---------------------------------------------------------------------


def test_form_shows_who_is_asking(client):
    _, req = start_authorization(client)
    r, csrf = open_form(client, req)
    assert r.status_code == 200
    assert "<b>Claude</b> wants read access" in r.text
    assert "<b>claude.ai</b>" in r.text  # where the owner will be sent back
    assert 'value="deny"' in r.text and csrf


def test_form_escapes_client_name(client):
    _, req = start_authorization(client, name="<script>alert(1)</script>")
    r, _ = open_form(client, req)
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text


def test_form_security_headers_and_cookie(client):
    _, req = start_authorization(client)
    r, _ = open_form(client, req)
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "script-src" not in r.headers["content-security-policy"]
    assert "default-src 'none'" in r.headers["content-security-policy"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie


def test_unknown_request_is_expired(client):
    r, _ = open_form(client, "bogus")
    assert r.status_code == 400 and "expired" in r.text


def test_direct_form_is_for_refreshing(client):
    r, _ = open_form(client)
    assert "refresh kcal's Garmin session" in r.text
    assert 'value="deny"' not in r.text


# --- signing in to connect a client ---------------------------------------------------


def test_owner_sign_in_completes_oauth(client, garmin, state):
    client_id, req = start_authorization(client)
    r = sign_in(client, req)
    assert r.status_code == 303
    back = r.headers["location"]
    assert back.startswith(REDIRECT) and query(back)["state"] == "st4te"
    assert (state / "garmin_tokens" / "saved.json").exists()
    # The code works, and the token belongs to the owner.
    tokens = client.post("/token", data={
        "grant_type": "authorization_code", "code": query(back)["code"],
        "redirect_uri": REDIRECT, "client_id": client_id,
        "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    }).json()
    r = client.post("/mcp", content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                    headers={"Authorization": f"Bearer {tokens['access_token']}",
                             "Content-Type": "application/json",
                             "Accept": "application/json, text/event-stream"})
    assert r.status_code == 200


def test_sign_in_resets_cached_session(client, monkeypatch):
    monkeypatch.setattr(mcp_server, "_api", object())
    _, req = start_authorization(client)
    sign_in(client, req)
    assert mcp_server._api is None  # next tool call loads the new tokens


def test_mfa_sign_in(client, garmin):
    _, req = start_authorization(client)
    r = sign_in(client, req, email="mfa@example.com")
    assert r.status_code == 200 and "verification code" in r.text
    csrf = client.cookies.get(login_page.CSRF_COOKIE)
    r = client.post("/login", data={
        "csrf": csrf, "flow": flow_id(r.text), "code": " 123456 ", "action": "mfa",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(REDIRECT)


def submit_code(client, flow, code, req=""):
    return client.post("/login", data={
        "csrf": client.cookies.get(login_page.CSRF_COOKIE), "flow": flow, "req": req,
        "code": code, "action": "mfa",
    }, follow_redirects=False)


def test_wrong_mfa_code_can_be_corrected(client, garmin):
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    r = submit_code(client, flow, "000000", req)
    # Garmin kept the step open: the same code form again, with the error.
    assert "Garmin didn't accept that code" in r.text and flow_id(r.text) == flow
    r = submit_code(client, flow, "123456", req)
    assert r.status_code == 303 and r.headers["location"].startswith(REDIRECT)
    assert [c for c in garmin.calls if c[0] == "finish"] == [
        ("finish", "000000"), ("finish", "123456"),
    ]


def test_wrong_code_starts_over_if_garmin_closed_the_step(client, garmin):
    garmin.wrong_code_closes_step = True
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    r = submit_code(client, flow, "000000", req)
    assert "didn't accept" in r.text and 'name="password"' in r.text  # the sign-in form
    r = submit_code(client, flow, "123456", req)
    assert r.status_code == 400 and "Sign-in expired" in r.text
    assert [c for c in garmin.calls if c[0] == "finish"] == [("finish", "000000")]


def test_repeated_wrong_code_is_replayed_not_resent(client, garmin):
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    first, second = submit_code(client, flow, "000000", req), submit_code(client, flow, "000000", req)
    assert first.text == second.text
    assert [c for c in garmin.calls if c[0] == "finish"] == [("finish", "000000")]


def test_wrong_codes_count_toward_the_limit(client, garmin):
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    for i in range(login_page.MAX_FAILURES):
        submit_code(client, flow, f"00000{i}", req)
    r = submit_code(client, flow, "123456", req)
    assert r.status_code == 429  # paused, though the step is still open
    assert ("finish", "123456") not in garmin.calls


def test_code_after_success_gets_the_same_redirect(client, garmin):
    # Two submissions with different codes (a correction typed while the
    # first was being checked): the browser shows the second response.
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    good = submit_code(client, flow, "123456", req)
    late = submit_code(client, flow, "999999", req)
    assert late.status_code == 303 and late.headers["location"] == good.headers["location"]
    assert ("finish", "999999") not in garmin.calls


def test_code_form_offers_start_over(client, garmin):
    _, req = start_authorization(client)
    r = sign_in(client, req, email="mfa@example.com")
    assert f'href="/login?req={req}">Start over' in r.text


def test_different_codes_reach_garmin_one_at_a_time(garmin, provider, state):
    import threading

    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    page = login_page.LoginPage(provider, OWNER, state / "garmin_tokens", lambda: None, garmin)
    app = Starlette(routes=[Route("/login", page.handle, methods=["GET", "POST"])])
    running, overlaps = [], []
    original = garmin.finish

    def slow_finish(session, code):
        running.append(code)
        if len(running) > 1:
            overlaps.append(code)
        threading.Event().wait(0.2)
        running.remove(code)
        original(session, code)

    garmin.finish = slow_finish

    async def two_codes():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=PUBLIC,
            cookies={login_page.CSRF_COOKIE: "t"},
        ) as c:
            r = await c.post("/login", data={"csrf": "t", "email": "mfa@example.com",
                                             "password": PASSWORD, "action": "signin"})
            flow = flow_id(r.text)
            return await asyncio.gather(*(
                c.post("/login", data={"csrf": "t", "flow": flow, "code": code, "action": "mfa"})
                for code in ("000000", "123456")
            ))

    responses = asyncio.run(two_codes())
    assert overlaps == []
    assert any("Garmin session refreshed" in r.text for r in responses)


def test_mfa_flow_expires(client, garmin, monkeypatch):
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    later = time.time() + login_page.MFA_SECONDS + 1
    monkeypatch.setattr(login_page.time, "time", lambda: later)
    r = client.post("/login", data={
        "csrf": client.cookies.get(login_page.CSRF_COOKIE), "flow": flow,
        "code": "123456", "action": "mfa",
    })
    assert r.status_code == 400


def test_wrong_password(client, garmin, provider):
    _, req = start_authorization(client)
    r = sign_in(client, req, password="wrong-pass")
    assert r.status_code == 200 and "didn't accept" in r.text
    assert "wrong-pass" not in r.text  # never echoed
    assert f'value="{EMAIL}"' in r.text  # email kept for retrying
    assert provider.pending(req) is not None  # still waiting


def test_other_garmin_account_is_refused(client, garmin, state, provider):
    _, req = start_authorization(client)
    r = sign_in(client, req, email="other@example.com")
    assert r.status_code == 200 and "isn't the one this server belongs to" in r.text
    assert not (state / "garmin_tokens").exists()
    assert provider.pending(req) is not None


def test_deny(client, provider):
    _, req = start_authorization(client)
    _, csrf = open_form(client, req)
    r = client.post("/login", data={"csrf": csrf, "req": req, "action": "deny"}, follow_redirects=False)
    assert r.status_code == 303
    assert query(r.headers["location"]) == {"error": "access_denied", "state": "st4te"}
    assert provider.pending(req) is None


def test_expired_request_at_sign_in(client, garmin, provider):
    _, req = start_authorization(client)
    _, csrf = open_form(client, req)
    provider.deny_authorization(req)  # gone
    r = sign_in(client, req, csrf=csrf)
    assert r.status_code == 400 and garmin.calls == []


# --- refreshing the Garmin session --------------------------------------------------


def test_direct_sign_in_refreshes_garmin_only(client, state, provider):
    r = sign_in(client)
    assert r.status_code == 200 and "Garmin session refreshed" in r.text
    assert (state / "garmin_tokens" / "saved.json").exists()
    assert provider._codes == {}  # no OAuth code issued


# --- CSRF ---------------------------------------------------------------------------


def test_post_without_csrf_cookie_is_refused(client, garmin):
    _, req = start_authorization(client)
    client.cookies.clear()
    r = client.post("/login", data={"csrf": "x", "req": req, "email": EMAIL,
                                    "password": PASSWORD, "action": "signin"})
    assert r.status_code == 400 and garmin.calls == []


def test_post_with_mismatched_csrf_is_refused(client, garmin):
    _, req = start_authorization(client)
    open_form(client, req)
    r = sign_in(client, req, csrf="not-the-cookie")
    assert r.status_code == 400 and garmin.calls == []


# --- rate limiting --------------------------------------------------------------------


def test_failures_are_capped_for_everyone(client, garmin):
    for _ in range(login_page.MAX_FAILURES):
        sign_in(client, password="wrong")
    starts = len(garmin.calls)
    r = sign_in(client)  # even the right password
    assert r.status_code == 429 and "paused" in r.text
    assert len(garmin.calls) == starts  # Garmin never contacted


def test_wrong_account_counts_as_failure(client, garmin):
    for _ in range(login_page.MAX_FAILURES):
        sign_in(client, email="other@example.com")
    assert sign_in(client).status_code == 429


def test_failures_expire_after_the_window(client, garmin, monkeypatch):
    for _ in range(login_page.MAX_FAILURES):
        sign_in(client, password="wrong")
    later = time.time() + login_page.FAILURE_WINDOW_SECONDS + 1
    monkeypatch.setattr(login_page.time, "time", lambda: later)
    assert sign_in(client).status_code == 200


def test_unreachable_garmin_is_not_counted(client, garmin):
    garmin.error = GarminConnectConnectionError("timeout")
    for _ in range(login_page.MAX_FAILURES + 1):
        r = sign_in(client)
    assert "Couldn't reach Garmin" in r.text
    garmin.error = None
    assert "Garmin session refreshed" in sign_in(client).text


def test_garmin_rate_limit_is_counted(client, garmin):
    garmin.error = GarminConnectTooManyRequestsError("429")
    for _ in range(login_page.MAX_FAILURES):
        r = sign_in(client)
    assert "limiting sign-ins" in r.text
    garmin.error = None
    assert sign_in(client).status_code == 429


# --- the Garmin-expired tool error ------------------------------------------------------


@pytest.fixture
def failing_login(monkeypatch):
    def fail(**_):
        raise GarminConnectAuthenticationError("token expired")
    monkeypatch.setattr(mcp_server, "_api", None)
    monkeypatch.setattr(mcp_server, "login", fail)


def tool_error(server, name, args):
    with pytest.raises(ToolError) as err:
        asyncio.run(server.call_tool(name, args))
    return str(err.value)


@pytest.mark.parametrize(
    "tool, args",
    [
        ("get_garmin_day", {"date": "2026-10-05"}),
        ("get_garmin_summary", {"date": "2026-10-05", "metrics": ["weight"]}),
        ("call_garmin_endpoint", {"endpoint": "get_sleep_data", "args": {"cdate": "d"}}),
    ],
)
def test_expired_garmin_session_says_what_to_do(server, failing_login, tool, args):
    message = tool_error(server, tool, args)
    assert "Garmin session expired" in message  # what happened
    assert "Nothing is wrong with the connector" in message
    assert f"open {PUBLIC}/login" in message  # the full URL
    assert "sign in to Garmin" in message and "verification code" in message  # what to do there
    assert "ask again" in message and "no need to reconnect" in message  # what to do after
    assert "show this link to the user" in message  # don't just retry


def test_stdio_keeps_its_own_message(failing_login):
    message = tool_error(mcp_server.mcp, "get_garmin_day", {"date": "2026-10-05"})
    assert "token expired" in message and "/login" not in message


class ExpiredSession:
    """A cached Garmin session that Garmin has since rejected: every request
    fails the way garminconnect reports a 401 it couldn't refresh past.
    get_user_summary answers for FIRST_DAY so the per-day groups' parallel
    workers are the ones to hit the rejection.
    """

    FIRST_DAY = "2026-10-03"

    @property
    def client(self):
        def profile(path):
            assert path == "/userprofile-service/socialProfile"
            return {"profileId": OWNER}
        return SimpleNamespace(connectapi=profile)

    def _rejected(self, *args, **kwargs):
        raise GarminConnectAuthenticationError("401 Unauthorized")

    get_sleep_data = get_hrv_data = get_activities_by_date = connectapi = _rejected
    get_hrv_data_range = get_sleep_daily = _rejected

    def get_user_summary(self, cdate):
        if cdate == self.FIRST_DAY:
            return {"restingHeartRate": 46}
        self._rejected()


@pytest.mark.parametrize(
    "tool, args",
    [
        ("get_garmin_day", {"date": "2026-10-05", "metrics": ["sleep_data", "hrv_data"]}),
        ("call_garmin_endpoint", {"endpoint": "get_sleep_data", "args": {"cdate": "2026-10-05"}}),
        # A range endpoint (one request for the range)...
        ("get_garmin_summary", {"date": "2026-10-05", "metrics": ["weight", "hrv"]}),
        # ...and the per-day groups, whose later days run in worker threads.
        ("get_garmin_summary", {"from_date": "2026-10-03", "to_date": "2026-10-06",
                                "metrics": ["heart"]}),
    ],
)
def test_session_rejected_mid_call(server, monkeypatch, tool, args):
    monkeypatch.setattr(mcp_server, "_api", ExpiredSession())
    message = tool_error(server, tool, args)
    assert "Garmin session expired" in message
    assert f"open {PUBLIC}/login" in message
    assert mcp_server._api is None  # the dead session is dropped


# --- kcal whoami ----------------------------------------------------------------------


def test_whoami_prints_profile_id(monkeypatch, capsys):
    class Client:
        def connectapi(self, path):
            assert path == "/userprofile-service/socialProfile"
            return {"profileId": OWNER, "displayName": "owner-display"}

    class Api:
        client = Client()
        display_name = "owner-display"

    monkeypatch.setattr(cli, "login", lambda: Api())
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert f"profile ID:   {OWNER}" in out and "owner-display" in out


# --- review fixes ------------------------------------------------------------------------


def test_sign_in_always_checks_the_password(monkeypatch, tmp_path):
    # Even with a token store configured, the form's email and password are
    # what logs in: garminconnect's Garmin.login() would load GARMINTOKENS
    # and skip them.
    monkeypatch.setenv("GARMINTOKENS", str(tmp_path))
    seen = []

    class Client:
        def login(self, email, password, return_on_mfa=False):
            seen.append((email, password, return_on_mfa))
            return "needs_mfa", None

    class StubGarmin:
        def __init__(self, **_):
            self.client = Client()

        def login(self, *_, **__):
            pytest.fail("Garmin.login() would consult the token store")

    monkeypatch.setattr(login_page, "Garmin", StubGarmin)
    _, needs_mfa = login_page.GarminGateway().start(EMAIL, PASSWORD)
    assert seen == [(EMAIL, PASSWORD, True)] and needs_mfa


def test_parallel_attempts_cannot_exceed_the_cap(garmin, provider, state):
    import threading

    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    release = threading.Event()
    original = garmin.start

    def slow_start(email, password):
        release.wait(5)
        return original(email, password)

    garmin.start = slow_start
    page = login_page.LoginPage(provider, OWNER, state / "garmin_tokens", lambda: None, garmin)
    app = Starlette(routes=[Route("/login", page.handle, methods=["GET", "POST"])])

    async def attack():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=PUBLIC, cookies={login_page.CSRF_COOKIE: "t"}
        ) as c:
            data = {"csrf": "t", "email": EMAIL, "password": "guess", "action": "signin"}
            tasks = [asyncio.create_task(c.post("/login", data=data)) for _ in range(20)]
            await asyncio.sleep(0.5)  # all requests are in: let Garmin answer
            release.set()
            return await asyncio.gather(*tasks)

    responses = asyncio.run(attack())
    # Only as many reach Garmin as the cap allows; the rest wait it out.
    assert len(garmin.calls) == login_page.MAX_FAILURES
    assert len([r for r in responses if r.status_code == 429]) == 20 - login_page.MAX_FAILURES


def test_lockout_keeps_a_waiting_mfa_step(garmin, provider, state):
    from starlette.applications import Starlette
    from starlette.routing import Route

    page = login_page.LoginPage(provider, OWNER, state / "garmin_tokens", lambda: None, garmin)
    app = Starlette(routes=[Route("/login", page.handle, methods=["GET", "POST"])])
    with TestClient(app, base_url=PUBLIC) as c:
        flow = flow_id(sign_in(c, email="mfa@example.com").text)
        page._failures.extend([time.time()] * login_page.MAX_FAILURES)
        data = {"csrf": c.cookies.get(login_page.CSRF_COOKIE), "flow": flow,
                "code": "123456", "action": "mfa"}
        assert c.post("/login", data=data).status_code == 429
        page._failures.clear()  # the pause ends
        r = c.post("/login", data=data)
        assert "Garmin session refreshed" in r.text  # the same code still works


def test_incomplete_session_is_not_saved(client, garmin, state):
    def save(session, token_store):
        raise login_page.IncompleteSession("no refresh token")

    garmin.save = save
    r = sign_in(client)
    assert "didn't issue a session kcal can keep" in r.text
    assert not (state / "garmin_tokens").exists()


def test_real_gateway_refuses_session_without_refresh_token(tmp_path):
    class Client:
        di_token, di_refresh_token = "access", None

        def dump(self, path):
            pytest.fail("would overwrite stored tokens")

    class Api:
        client = Client()

    with pytest.raises(login_page.IncompleteSession):
        login_page.GarminGateway().save(Api(), tmp_path)


def test_unwritable_token_store_is_explained(client, garmin):
    def save(session, token_store):
        raise PermissionError("locked")

    garmin.save = save
    r = sign_in(client)
    assert r.status_code == 200 and "couldn't save the Garmin session" in r.text


def test_cookie_not_secure_on_plain_http(garmin, state):
    p = KcalOAuthProvider(state / "a.sqlite", "http://localhost:8000")
    server = mcp_server.build_server(
        HttpSettings(public_url="http://localhost:8000", garmin_owner=OWNER), p
    )
    with TestClient(server.streamable_http_app(), base_url="http://localhost:8000") as c:
        r = c.get("/login")
    assert "secure" not in r.headers["set-cookie"].lower()
    p.close()


@pytest.mark.parametrize(
    "error", [GarminConnectConnectionError("timeout"), GarminConnectTooManyRequestsError("429")]
)
def test_garmin_outage_is_not_reported_as_expired(server, monkeypatch, error):
    def fail(**_):
        raise error

    monkeypatch.setattr(mcp_server, "_api", None)
    monkeypatch.setattr(mcp_server, "login", fail)
    message = tool_error(server, "get_garmin_day", {"date": "2026-10-05"})
    assert "try again in a few minutes" in message
    assert "/login" not in message and "expired" not in message


# --- double submissions and ways out of error pages ---------------------------------------


def test_double_submitted_code_still_gets_back_to_the_client(client, garmin):
    # What happened live: Verify submitted twice. The browser shows the second
    # response, which must be the same redirect, not "expired".
    _, req = start_authorization(client)
    r = sign_in(client, req, email="mfa@example.com")
    data = {"csrf": client.cookies.get(login_page.CSRF_COOKIE), "flow": flow_id(r.text),
            "req": req, "code": "123456", "action": "mfa"}
    first = client.post("/login", data=data, follow_redirects=False)
    second = client.post("/login", data=data, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    assert [c for c in garmin.calls if c[0] == "finish"] == [("finish", "123456")]


def test_concurrent_double_submit_waits_for_the_first(garmin, provider, state):
    import threading

    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    page = login_page.LoginPage(provider, OWNER, state / "garmin_tokens", lambda: None, garmin)
    app = Starlette(routes=[Route("/login", page.handle, methods=["GET", "POST"])])
    release = threading.Event()
    original = garmin.finish

    def slow_finish(session, code):
        release.wait(5)
        original(session, code)

    garmin.finish = slow_finish

    async def double_click():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=PUBLIC,
            cookies={login_page.CSRF_COOKIE: "t"},
        ) as c:
            r = await c.post("/login", data={"csrf": "t", "email": "mfa@example.com",
                                             "password": PASSWORD, "action": "signin"})
            data = {"csrf": "t", "flow": flow_id(r.text), "code": "123456", "action": "mfa"}
            tasks = [asyncio.create_task(c.post("/login", data=data)) for _ in range(2)]
            await asyncio.sleep(0.3)
            release.set()
            return await asyncio.gather(*tasks)

    first, second = asyncio.run(double_click())
    assert "Garmin session refreshed" in first.text and second.text == first.text
    assert len([c for c in garmin.calls if c[0] == "finish"]) == 1


def test_double_submitted_sign_in_logs_in_once(client, garmin):
    _, req = start_authorization(client)
    r, csrf = open_form(client, req)
    attempt = r.text.split('name="attempt" value="')[1].split('"')[0]
    data = {"csrf": csrf, "req": req, "attempt": attempt, "email": "mfa@example.com",
            "password": PASSWORD, "action": "signin"}
    first, second = client.post("/login", data=data), client.post("/login", data=data)
    assert flow_id(first.text) == flow_id(second.text)
    assert [c for c in garmin.calls if c[0] == "start"] == [("start", "mfa@example.com")]


def test_each_form_is_a_new_attempt(client, garmin):
    # Retrying from a freshly loaded form really retries.
    sign_in(client, password="wrong")
    sign_in(client)
    assert len([c for c in garmin.calls if c[0] == "start"]) == 2


def test_expired_code_step_offers_to_start_again(client, garmin):
    _, req = start_authorization(client)
    r = client.post("/login", data={
        "csrf": open_form(client, req)[1], "flow": "gone", "req": req,
        "code": "1", "action": "mfa",
    })
    assert r.status_code == 400 and "Sign-in expired" in r.text
    assert f'href="/login?req={req}">Start again' in r.text
    assert 'value="deny">Cancel and go back to claude.ai' in r.text


def test_expired_request_offers_the_way_back(client, garmin, provider, monkeypatch):
    from kcal import oauth
    _, req = start_authorization(client)
    csrf = open_form(client, req)[1]
    later = time.time() + oauth.PENDING_SECONDS + 1
    monkeypatch.setattr(oauth.time, "time", lambda: later)
    r = client.get("/login", params={"req": req})
    assert r.status_code == 400 and "Back to claude.ai to connect again" in r.text
    r = client.post("/login", data={"csrf": csrf, "req": req, "action": "return"},
                    follow_redirects=False)
    assert r.status_code == 303
    back = query(r.headers["location"])
    assert back["error"] == "access_denied" and back["state"] == "st4te"
    assert "expired" in back["error_description"]
    assert r.headers["location"].startswith(REDIRECT)
    # Single use; afterwards the page can only point back to the app.
    r = client.post("/login", data={"csrf": csrf, "req": req, "action": "return"})
    assert "Go back to Claude or ChatGPT" in r.text


def test_unknown_request_points_back_to_the_app(client):
    r, _ = open_form(client, "never-existed")
    assert "Go back to Claude or ChatGPT and connect again" in r.text


def test_expired_refresh_offers_to_start_again(client):
    r = client.post("/login", data={
        "csrf": open_form(client)[1], "flow": "gone", "code": "1", "action": "mfa",
    })
    assert 'href="/login">Start again' in r.text


def test_expired_form_offers_a_way_on(client):
    _, req = start_authorization(client)
    open_form(client, req)
    r = client.post("/login", data={"csrf": "stale", "req": req, "action": "signin"})
    assert r.status_code == 400 and f'href="/login?req={req}">Start again' in r.text


def test_lockout_page_offers_cancel(client, garmin):
    _, req = start_authorization(client)
    for _ in range(login_page.MAX_FAILURES):
        sign_in(client, req, password="wrong")
    r = sign_in(client, req)
    assert r.status_code == 429 and "Cancel and go back to claude.ai" in r.text


def test_expired_requests_are_forgotten_after_an_hour(client, provider, monkeypatch):
    from kcal import oauth
    _, req = start_authorization(client)
    later = time.time() + oauth.PENDING_SECONDS + oauth.RECENT_SECONDS + 1
    monkeypatch.setattr(oauth.time, "time", lambda: later)
    start_authorization(client)  # prunes
    assert provider.expired(req) is None and provider.return_to_client(req) is None


def test_completing_an_expired_request_is_refused(client, provider, monkeypatch):
    from kcal import oauth
    _, req = start_authorization(client)
    later = time.time() + oauth.PENDING_SECONDS + 1
    monkeypatch.setattr(oauth.time, "time", lambda: later)
    with pytest.raises(KeyError):
        provider.complete_authorization(req, "owner")
    assert provider.expired(req) is not None  # still there for the way back


# --- the whole logout and re-login flow ------------------------------------------------


def mcp_tool_call(client, token, name, arguments):
    """A tools/call over HTTP, authorized like Claude's: (is_error, text)."""
    r = client.post(
        "/mcp",
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}}),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    return result.get("isError", False), result["content"][0]["text"]


def test_garmin_logout_and_sign_in_again(client, garmin, state, monkeypatch):
    """Connected client; Garmin then logs the owner out; the tool says where
    to sign in; the owner signs in at that link; the same client, with the
    same token, works again. Garmin itself is faked: its tokens are valid
    while their generation matches Garmin's current one.
    """
    garmin_side = {"generation": 1}
    token_file = state / "garmin_tokens" / "saved.json"

    def save(session, token_store):  # what /login stores after a sign-in
        token_store.mkdir(parents=True, exist_ok=True)
        token_file.write_text(json.dumps({"generation": garmin_side["generation"]}))

    garmin.save = save

    class Session:
        def __init__(self, generation):
            self.generation = generation

        @property
        def client(self):
            def profile(path):
                assert path == "/userprofile-service/socialProfile"
                self.get_sleep_data("profile-check")
                return {"profileId": OWNER}
            return SimpleNamespace(connectapi=profile)

        def get_sleep_data(self, cdate):
            if self.generation != garmin_side["generation"]:
                raise GarminConnectAuthenticationError("401 Unauthorized")
            return {"dailySleepDTO": {"calendarDate": cdate, "sleepTimeSeconds": 25000}}

    def login(**_):  # what the server does to load the stored tokens
        if not token_file.exists():
            raise GarminConnectAuthenticationError("no tokens")
        session = Session(json.loads(token_file.read_text())["generation"])
        session.get_sleep_data("check")  # garminconnect checks the tokens on load
        return session

    monkeypatch.setattr(mcp_server, "login", login)
    monkeypatch.setattr(mcp_server, "_api", None)
    ask = {"date": "2026-10-05", "metrics": ["sleep_data"]}

    # 1. Connect Claude: authorize, sign in to Garmin, exchange the code.
    client_id, req = start_authorization(client)
    back = sign_in(client, req).headers["location"]
    tokens = client.post("/token", data={
        "grant_type": "authorization_code", "code": query(back)["code"],
        "redirect_uri": REDIRECT, "client_id": client_id,
        "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    }).json()
    is_error, text = mcp_tool_call(client, tokens["access_token"], "get_garmin_day", ask)
    assert not is_error and json.loads(text)["sleep_data"]["dailySleepDTO"]["sleepTimeSeconds"]

    # 2. Garmin logs the owner out (password change, security reset, ...).
    garmin_side["generation"] += 1

    # 3. The next call explains what to do, with the link.
    is_error, text = mcp_tool_call(client, tokens["access_token"], "get_garmin_day", ask)
    assert is_error and "Garmin session expired" in text
    link = re.search(r"open (\S+/login)", text).group(1)
    assert link == f"{PUBLIC}/login"
    # Asking again before signing in fails the same way (no silent recovery).
    is_error, _ = mcp_tool_call(client, tokens["access_token"], "get_garmin_day", ask)
    assert is_error

    # 4. The owner opens the link and signs in to Garmin again.
    r = client.get(urlsplit(link).path)
    assert "refresh kcal's Garmin session" in r.text
    r = sign_in(client)
    assert "Garmin session refreshed" in r.text

    # 5. Asking again works, with the same client token: no reconnecting.
    is_error, text = mcp_tool_call(client, tokens["access_token"], "get_garmin_day", ask)
    assert not is_error and json.loads(text)["sleep_data"]["dailySleepDTO"]["calendarDate"] == "2026-10-05"


@pytest.mark.parametrize("mfa", [False, True])
def test_slow_owner_checks_still_reserve_attempt_slots(garmin, provider, state, mfa):
    import queue
    import threading
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    garmin.accounts = {**garmin.accounts, "other-mfa@example.com": (PASSWORD, 999, "123456")}
    release = threading.Event()
    entered = queue.Queue()
    original = garmin.profile_id

    def slow_profile(session):
        entered.put(True)
        assert release.wait(10), "test did not release profile lookup"
        return original(session)

    garmin.profile_id = slow_profile
    page = login_page.LoginPage(provider, OWNER, state / "garmin_tokens", lambda: None, garmin)
    app = Starlette(routes=[Route("/login", page.handle, methods=["GET", "POST"])])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC,
                                     cookies={login_page.CSRF_COOKIE: "t"}) as c:
            submissions = []
            for _ in range(login_page.MAX_FAILURES + 1):
                data = {"csrf": "t", "email": "other@example.com", "password": PASSWORD}
                if mfa:
                    r = await c.post("/login", data={**data, "email": "other-mfa@example.com"})
                    assert r.status_code == 200
                    data = {"csrf": "t", "action": "mfa", "flow": flow_id(r.text), "code": "123456"}
                submissions.append(data)
            tasks = [asyncio.create_task(c.post("/login", data=data)) for data in submissions[:-1]]
            try:
                for _ in tasks:
                    await asyncio.wait_for(asyncio.to_thread(entered.get, True, 5), timeout=6)
                assert page._in_flight == login_page.MAX_FAILURES
                blocked = await c.post("/login", data=submissions[-1])
                assert blocked.status_code == 429
            finally:
                release.set()
                responses = await asyncio.gather(*tasks)
            assert all(r.status_code == 200 and "isn't the one" in r.text for r in responses)
            assert page._in_flight == 0
            assert len(page._failures) == login_page.MAX_FAILURES
            assert not (state / "garmin_tokens").exists()
            assert len([x for x in garmin.calls if x[0] == ("finish" if mfa else "start")]) == login_page.MAX_FAILURES

    asyncio.run(run())



# --- retrying the code: review fixes --------------------------------------------------


@pytest.mark.parametrize("failure", [
    GarminConnectConnectionError("timeout"), GarminConnectTooManyRequestsError("429"),
])
def test_same_code_is_tried_again_after_a_temporary_failure(client, garmin, failure):
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    garmin.finish_failures = [failure]
    r = submit_code(client, flow, "123456", req)
    assert flow_id(r.text) == flow  # the code form again
    # Garmin has recovered: the very same code must reach it, not a replay.
    r = submit_code(client, flow, "123456", req)
    assert r.status_code == 303 and r.headers["location"].startswith(REDIRECT)
    assert [c for c in garmin.calls if c[0] == "finish"] == [("finish", "123456")] * 2


def test_success_beats_a_remembered_wrong_code(client, garmin):
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    submit_code(client, flow, "000000", req)  # wrong: remembered
    good = submit_code(client, flow, "123456", req)
    again = submit_code(client, flow, "000000", req)  # e.g. a stale tab resubmitting
    assert again.status_code == 303 and again.headers["location"] == good.headers["location"]
    assert [c for c in garmin.calls if c[0] == "finish"] == [
        ("finish", "000000"), ("finish", "123456"),
    ]


def test_code_queued_past_expiry_is_refused(garmin, provider, state, monkeypatch):
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    page = login_page.LoginPage(provider, OWNER, state / "garmin_tokens", lambda: None, garmin)
    app = Starlette(routes=[Route("/login", page.handle, methods=["GET", "POST"])])

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=PUBLIC,
            cookies={login_page.CSRF_COOKIE: "t"},
        ) as c:
            r = await c.post("/login", data={"csrf": "t", "email": "mfa@example.com",
                                             "password": PASSWORD, "action": "signin"})
            flow = flow_id(r.text)
            step = page._mfa[flow]
            await step.lock.acquire()  # another code is being checked
            queued = asyncio.create_task(c.post("/login", data={
                "csrf": "t", "flow": flow, "code": "123456", "action": "mfa"}))
            await asyncio.sleep(0.2)  # the submission is now waiting for its turn
            later = time.time() + login_page.MFA_SECONDS + 1
            monkeypatch.setattr(login_page.time, "time", lambda: later)
            step.lock.release()
            return await queued

    r = asyncio.run(scenario())
    assert r.status_code == 400 and "Sign-in expired" in r.text
    assert [c for c in garmin.calls if c[0] == "finish"] == []  # never reached Garmin
    assert not (state / "garmin_tokens").exists()



# --- Garmin unreachable during the code check; simultaneous retries ---------------------

# What GarminGateway.finish raises when Garmin's code check didn't answer
# (tests/test_garmin_library.py checks it against the real client).
OUTAGE = login_page.GarminVerificationUnavailable("Garmin didn't answer the code check")


def test_unreachable_verification_is_not_a_wrong_code(client, garmin):
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    garmin.finish_failures = [OUTAGE]
    r = submit_code(client, flow, "123456", req)
    assert "Couldn't reach Garmin" in r.text and "didn't accept" not in r.text
    r = submit_code(client, flow, "123456", req)  # Garmin is back
    assert r.status_code == 303
    assert [c for c in garmin.calls if c[0] == "finish"] == [("finish", "123456")] * 2


def test_unreachable_verification_does_not_count_toward_the_limit(client, garmin):
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    garmin.finish_failures = [OUTAGE] * (login_page.MAX_FAILURES + 1)
    for _ in range(login_page.MAX_FAILURES + 1):
        r = submit_code(client, flow, "123456", req)
    assert r.status_code == 200 and "Couldn't reach Garmin" in r.text  # never paused


def test_simultaneous_retries_after_a_temporary_failure(garmin, provider, state):
    # Three identical submissions while the first hits a temporary failure:
    # all get the first one's answer, Garmin is asked once, nothing crashes.
    import threading

    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    page = login_page.LoginPage(provider, OWNER, state / "garmin_tokens", lambda: None, garmin)
    app = Starlette(routes=[Route("/login", page.handle, methods=["GET", "POST"])])
    original = garmin.finish

    def slow_failure(session, code):
        threading.Event().wait(0.3)
        garmin.finish = original
        garmin.calls.append(("finish", code))
        raise GarminConnectConnectionError("timeout")

    async def three_at_once():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=PUBLIC,
            cookies={login_page.CSRF_COOKIE: "t"},
        ) as c:
            r = await c.post("/login", data={"csrf": "t", "email": "mfa@example.com",
                                             "password": PASSWORD, "action": "signin"})
            flow = flow_id(r.text)
            garmin.finish = slow_failure
            data = {"csrf": "t", "flow": flow, "code": "123456", "action": "mfa"}
            responses = await asyncio.gather(*(c.post("/login", data=data) for _ in range(3)))
            later = await c.post("/login", data=data)  # after the failure: really tried
            return responses, later

    responses, later = asyncio.run(three_at_once())
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len({r.text for r in responses}) == 1 and "Couldn't reach Garmin" in responses[0].text
    assert "Garmin session refreshed" in later.text
    assert [c for c in garmin.calls if c[0] == "finish"] == [("finish", "123456")] * 2


def test_replay_entries_are_only_removed_by_their_owner(garmin, provider, state):
    # If the first submission crashes, the duplicates waiting on it each try
    # for themselves; one finishing must not remove the other's entry.
    from starlette.responses import HTMLResponse

    page = login_page.LoginPage(provider, OWNER, state / "garmin_tokens", lambda: None, garmin)
    runs = []

    async def run():
        runs.append(len(runs))
        if runs[-1] == 0:
            await asyncio.sleep(0.05)
            raise RuntimeError("crashed")
        await asyncio.sleep(0.1 * runs[-1])  # the second waiter finishes last
        response = HTMLResponse("temporary failure")
        response.kcal_replayable = False
        return response

    async def three():
        return await asyncio.gather(
            *(page._once("mfa:step:code", run) for _ in range(3)), return_exceptions=True
        )

    first, second, third = asyncio.run(three())
    assert isinstance(first, RuntimeError)
    assert second.status_code == third.status_code == 200  # no KeyError
    assert page._submissions == {}



def test_flow_removed_during_a_failing_check_is_not_a_crash(garmin, provider, state):
    # While a code is being checked, another submission can remove the
    # expired step; the failing check must still answer, not crash.
    import threading

    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Route

    garmin.wrong_code_closes_step = True
    page = login_page.LoginPage(provider, OWNER, state / "garmin_tokens", lambda: None, garmin)
    app = Starlette(routes=[Route("/login", page.handle, methods=["GET", "POST"])])
    checking, release = threading.Event(), threading.Event()
    original = garmin.finish

    def slow_finish(session, code):
        checking.set()
        release.wait(5)
        original(session, code)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=PUBLIC,
            cookies={login_page.CSRF_COOKIE: "t"},
        ) as c:
            r = await c.post("/login", data={"csrf": "t", "email": "mfa@example.com",
                                             "password": PASSWORD, "action": "signin"})
            flow = flow_id(r.text)
            garmin.finish = slow_finish
            pending = asyncio.create_task(c.post("/login", data={
                "csrf": "t", "flow": flow, "code": "000000", "action": "mfa"}))
            while not checking.is_set():
                await asyncio.sleep(0.01)
            page._mfa.pop(flow)  # removed meanwhile, as an expiry sweep does
            release.set()
            return await pending

    r = asyncio.run(scenario())
    assert r.status_code == 200 and "didn't accept" in r.text and 'name="password"' in r.text


def test_finished_step_is_not_replayed_after_it_expires(client, garmin, monkeypatch):
    _, req = start_authorization(client)
    flow = flow_id(sign_in(client, req, email="mfa@example.com").text)
    assert submit_code(client, flow, "123456", req).status_code == 303
    later = time.time() + login_page.MFA_SECONDS + 1
    monkeypatch.setattr(login_page.time, "time", lambda: later)
    # An old tab resubmitting much later: no stale redirect, an expiry page.
    for code in ("123456", "999999"):
        r = submit_code(client, flow, code, req)
        assert r.status_code == 400 and "Sign-in expired" in r.text
