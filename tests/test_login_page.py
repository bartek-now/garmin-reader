"""The /login page (design sections 2 and 3), with a fake Garmin."""

import asyncio
import json
import time
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

    def start(self, email, password):
        self.calls.append(("start", email))
        if self.error:
            raise self.error
        account = self.accounts.get(email)
        if account is None or account[0] != password:
            raise GarminConnectAuthenticationError("401 Unauthorized")
        session = {"email": email, "profile": account[1], "mfa": account[2], "done": not account[2]}
        return session, account[2] is not None

    def finish(self, session, code):
        self.calls.append(("finish", code))
        if code != session["mfa"]:
            raise GarminConnectAuthenticationError("bad MFA code")
        session["done"] = True

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


def test_wrong_mfa_code_starts_over(client, garmin):
    _, req = start_authorization(client)
    r = sign_in(client, req, email="mfa@example.com")
    flow = flow_id(r.text)
    csrf = client.cookies.get(login_page.CSRF_COOKIE)
    r = client.post("/login", data={"csrf": csrf, "flow": flow, "code": "000000", "action": "mfa"})
    assert "didn't accept" in r.text
    # Garmin's MFA state is single use: submitting the flow again only shows
    # the same answer, and Garmin isn't asked twice.
    r = client.post("/login", data={"csrf": csrf, "flow": flow, "code": "123456", "action": "mfa"})
    assert "didn't accept" in r.text
    assert [c for c in garmin.calls if c[0] == "finish"] == [("finish", "000000")]


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


def test_session_rejected_mid_call(server, monkeypatch):
    class Expired:
        def get_sleep_data(self, cdate):
            raise GarminConnectAuthenticationError("401")

    monkeypatch.setattr(mcp_server, "_api", Expired())
    message = tool_error(server, "get_garmin_day", {"date": "d", "metrics": ["sleep_data", "hrv_data"]})
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
