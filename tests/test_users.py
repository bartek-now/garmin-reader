"""Several users on one HTTP server: each tool call reads only the Garmin
session of the user its OAuth token belongs to.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from garmin_reader import auth, mcp_server
from garmin_reader.oauth import GarminReaderOAuthProvider
from garmin_reader.settings import HttpSettings, user_token_store, users_dir
from signed_in import as_user

PUBLIC = "https://abc-def.trycloudflare.com"  # as in test_oauth, whose flow is reused
ME, FRIEND = "me@example.com", "friend@example.com"
PROFILES = {ME: 1001, FRIEND: 2002}
USERS = frozenset(PROFILES)
SLEEP = {"endpoint": "get_sleep_data", "args": {"cdate": "2026-10-05"}}


class Session:
    """A Garmin session for one profile; its sleep data says whose it is."""

    def __init__(self, profile):
        self.owner = profile
        self.profiles = []
        self.health = []
        self.client = SimpleNamespace(connectapi=self.profile)

    def profile(self, path):
        self.profiles.append(path)
        assert path == "/userprofile-service/socialProfile"
        return {"profileId": self.owner}

    def get_sleep_data(self, cdate):
        self.health.append(cdate)
        return {"profile": self.owner}


@pytest.fixture
def provider(tmp_path):
    p = GarminReaderOAuthProvider(tmp_path / "mcp_auth.sqlite", PUBLIC)
    yield p
    p.close()


@pytest.fixture
def server(provider):
    server = mcp_server.build_server(HttpSettings(public_url=PUBLIC), provider)
    for email, profile in PROFILES.items():
        provider.set_garmin_profile(email, profile)
    return server


@pytest.fixture
def stores(monkeypatch):
    """login() loads the session stored for whichever profile it's given."""
    logins = []
    sessions = {}

    def login(token_store, **kwargs):
        logins.append((token_store, kwargs))
        profile = int(token_store.parent.name)
        assert token_store == user_token_store(profile)
        return sessions.setdefault(profile, Session(profile))

    monkeypatch.setattr(mcp_server, "login", login)
    return SimpleNamespace(logins=logins, sessions=sessions)


def sleep_as(server, email):
    with as_user(email):
        return json.loads(asyncio.run(server.call_tool("call_garmin_endpoint", SLEEP))[0].text)


def error_as(server, email):
    with as_user(email):
        with pytest.raises(ToolError) as err:
            asyncio.run(server.call_tool("call_garmin_endpoint", SLEEP))
    return str(err.value)


# --- isolation ---------------------------------------------------------------------


def test_each_user_reads_only_their_own_session(server, stores):
    assert sleep_as(server, ME) == {"profile": 1001}
    assert sleep_as(server, FRIEND) == {"profile": 2002}
    assert sleep_as(server, ME) == {"profile": 1001}
    assert [store for store, _ in stores.logins] == [user_token_store(1001), user_token_store(2002)]
    assert stores.sessions[1001].health == ["2026-10-05"] * 2
    assert stores.sessions[2002].health == ["2026-10-05"]


def test_users_reach_their_own_data_over_http(server, provider, stores):
    from test_oauth import connect, mcp_call  # the real OAuth flow, as Claude does it

    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as client:
        tokens = {email: connect(client, provider, user=email)[1] for email in PROFILES}

        def sleep(email):
            r = client.post("/mcp", content=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "call_garmin_endpoint", "arguments": SLEEP},
            }), headers={
                "Authorization": f"Bearer {tokens[email]['access_token']}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            })
            assert r.status_code == 200, r.text
            return json.loads(r.json()["result"]["content"][0]["text"])

        assert sleep(FRIEND) == {"profile": 2002}
        assert sleep(ME) == {"profile": 1001}
        assert mcp_call(client, tokens[ME]["access_token"]).status_code == 200


def test_call_without_a_user_never_falls_back_to_a_session(server, stores, monkeypatch):
    monkeypatch.setattr(mcp_server, "_api", Session(1001))  # e.g. a stdio session
    with pytest.raises(ToolError, match="isn't signed in"):
        asyncio.run(server.call_tool("call_garmin_endpoint", SLEEP))
    assert stores.logins == [] and mcp_server._api.health == []


def test_user_who_never_signed_in_to_garmin_is_sent_to_login(server, provider, stores):
    with provider._lock:
        provider._db.execute("DELETE FROM users WHERE email = ?", (FRIEND,))
    message = error_as(server, FRIEND)
    assert "Garmin session expired" in message and f"{PUBLIC}/login" in message
    assert stores.logins == []


def test_one_users_rejected_session_leaves_the_others(server, stores):
    assert sleep_as(server, ME) == {"profile": 1001}
    assert sleep_as(server, FRIEND) == {"profile": 2002}

    def rejected(cdate):
        raise GarminConnectAuthenticationError("401 Unauthorized")

    stores.sessions[2002].get_sleep_data = rejected
    entry = mcp_server._profile_sessions[2002]
    assert "Garmin session expired" in error_as(server, FRIEND)
    # Cleared in place: the entry and its login lock stay the same.
    assert mcp_server._profile_sessions[2002] is entry and entry.api is None
    assert mcp_server._profile_sessions[1001].api is stores.sessions[1001]


def test_user_sessions_never_use_the_environments_credentials(server, stores):
    sleep_as(server, ME)
    assert stores.logins[0][1]["env_credentials"] is False


def test_login_ignores_environment_credentials_when_told(monkeypatch, tmp_path):
    seen = []

    class StubGarmin:
        def __init__(self, email=None, password=None, **_):
            seen.append((email, password))
            self.client = SimpleNamespace(
                _refresh_session=lambda *a, **k: None,
                _token_lock=__import__("threading").RLock(),
                login=lambda *a, **k: None,
            )

        def login(self, tokenstore):
            pass

    monkeypatch.setattr(auth, "Garmin", StubGarmin)
    monkeypatch.setenv("GARMIN_EMAIL", "someone@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "their-password")
    auth.login(token_store=tmp_path / "tokens", env_credentials=False)
    auth.login(token_store=tmp_path / "tokens")
    assert seen == [(None, None), ("someone@example.com", "their-password")]


# --- the stored session must be the user's own --------------------------------------------


@pytest.mark.parametrize("cached", [False, True])
def test_session_of_another_account_is_rejected_before_health_data(
    server, provider, monkeypatch, cached,
):
    wrong = Session(2002)  # FRIEND's account, somehow in ME's store
    monkeypatch.setattr(mcp_server, "login", lambda **kwargs: wrong)
    if cached:
        mcp_server._profile_sessions[1001] = mcp_server._ProfileSession(
            api=wrong, revision=provider.garmin_session(ME)[1]
        )
    message = error_as(server, ME)
    assert "different account" in message and PUBLIC + "/login" in message
    assert wrong.profiles == ["/userprofile-service/socialProfile"]
    assert wrong.health == []
    assert mcp_server._profile_sessions[1001].api is None


def test_profile_verified_once_per_session_and_again_after_reload(server, provider, stores):
    for _ in range(2):
        assert sleep_as(server, ME) == {"profile": 1001}
    assert len(stores.logins) == 1
    assert stores.sessions[1001].profiles == ["/userprofile-service/socialProfile"]
    provider.set_garmin_profile(ME, 1001)  # ME signed in again: a new revision
    stores.sessions[1001] = Session(2002)  # the store now holds another account
    assert "different account" in error_as(server, ME)
    assert len(stores.logins) == 2
    assert stores.sessions[1001].health == []


def test_profile_outage_does_not_verify_or_discard_the_session(server, stores):
    session = stores.sessions[1001] = Session(1001)

    def unavailable(path):
        raise GarminConnectConnectionError("temporarily unavailable")

    session.client.connectapi = unavailable
    message = error_as(server, ME)
    assert "try again in a few minutes" in message and "/login" not in message
    cached = mcp_server._profile_sessions[1001]
    assert cached.api is session and not cached.verified
    assert session.health == []
    session.client.connectapi = session.profile
    assert sleep_as(server, ME) == {"profile": 1001}
    assert len(stores.logins) == 1


def test_two_emails_of_one_account_share_one_session(server, provider, stores):
    # E.g. the account's login email changed: two clients rotating one
    # refresh token would sign each other out.
    provider.set_garmin_profile(FRIEND, 1001)
    assert sleep_as(server, ME) == {"profile": 1001}
    assert sleep_as(server, FRIEND) == {"profile": 1001}
    assert [store for store, _ in stores.logins] == [user_token_store(1001)]
    assert set(mcp_server._profile_sessions) == {1001}


def test_session_replaced_by_another_process_is_reloaded(server, provider, stores, tmp_path):
    assert sleep_as(server, ME) == {"profile": 1001}
    assert sleep_as(server, ME) == {"profile": 1001}
    assert len(stores.logins) == 1
    # e.g. `garmin-reader-mcp --import-garmin-session` while the server runs
    other = GarminReaderOAuthProvider(tmp_path / "mcp_auth.sqlite", PUBLIC)
    try:
        other.set_garmin_profile(ME, 1001)
    finally:
        other.close()
    stores.sessions[1001] = imported = Session(1001)
    assert sleep_as(server, ME) == {"profile": 1001}
    assert len(stores.logins) == 2 and imported.health == ["2026-10-05"]


def test_changed_profile_loads_the_new_store(server, provider, stores):
    assert sleep_as(server, ME) == {"profile": 1001}
    provider.set_garmin_profile(ME, 3003)  # signed in with another Garmin account
    assert sleep_as(server, ME) == {"profile": 3003}
    assert [store for store, _ in stores.logins] == [user_token_store(1001), user_token_store(3003)]


# --- removing a user -----------------------------------------------------------------------


# --- importing a session signed in elsewhere ----------------------------------------------


class Importable(Session):
    def __init__(self, profile, refresh_token="refresh"):
        super().__init__(profile)
        self.client.dump = self.dump
        self.client.di_token, self.client.di_refresh_token = "access", refresh_token
        self.dumped = []

    def dump(self, path):
        self.dumped.append(path)


def test_import_stores_the_session_as_that_users(monkeypatch, tmp_path, capsys):
    session = Importable(2002)
    seen = []

    def login(token_store, **kwargs):
        seen.append((token_store, kwargs))
        return session

    monkeypatch.setattr(mcp_server, "login", login)
    assert mcp_server.main(["--import-garmin-session", "Friend@Example.com", str(tmp_path)]) == 0
    assert seen == [(tmp_path, {"prompt_mfa": mcp_server._no_mfa, "env_credentials": False})]
    assert session.dumped == [str(user_token_store(2002))]
    from garmin_reader.settings import state_dir
    provider = GarminReaderOAuthProvider(state_dir() / "mcp_auth.sqlite", PUBLIC)
    try:
        assert provider.garmin_profile(FRIEND) == 2002
        assert provider.garmin_profile(ME) is None
    finally:
        provider.close()
    assert "as friend@example.com's (Garmin profile 2002)" in capsys.readouterr().err


def test_import_refuses_a_session_that_cant_be_renewed(monkeypatch, tmp_path, capsys):
    session = Importable(2002, refresh_token=None)
    monkeypatch.setattr(mcp_server, "login", lambda **_: session)
    assert mcp_server.main(["--import-garmin-session", FRIEND, str(tmp_path)]) == 1
    assert "reusable tokens" in capsys.readouterr().err
    assert session.dumped == []
    from garmin_reader.settings import state_dir
    assert not (state_dir() / "mcp_auth.sqlite").exists()  # no new revision either




def test_import_reports_an_unusable_session(monkeypatch, tmp_path, capsys):
    def login(**_):
        raise GarminConnectAuthenticationError("token expired")

    monkeypatch.setattr(mcp_server, "login", login)
    assert mcp_server.main(["--import-garmin-session", ME, str(tmp_path)]) == 1
    assert "couldn't use the Garmin session" in capsys.readouterr().err
    from garmin_reader.settings import state_dir
    assert not (state_dir() / "mcp_auth.sqlite").exists()


def test_parallel_day_workers_stay_with_the_user(server, stores, monkeypatch):
    # get_garmin_summary fetches per-day groups in worker threads; each must
    # still use the calling user's session.
    seen = []

    def user_summary(self, cdate):
        seen.append(self.owner)
        return {"restingHeartRate": 40 + self.owner % 100}

    monkeypatch.setattr(Session, "get_user_summary", user_summary, raising=False)
    monkeypatch.setattr(mcp_server, "_api", Session(9999))  # must never be used
    with as_user(FRIEND):
        out = json.loads(asyncio.run(server.call_tool("get_garmin_summary", {
            "from_date": "2026-10-01", "to_date": "2026-10-06", "metrics": ["heart"],
        }))[0].text)
    assert seen == [2002] * 6
    column = out["fields"].index("resting_hr")
    assert {row[column] for row in out["days"]} == {42}
    assert [store for store, _ in stores.logins] == [user_token_store(2002)]


def test_reset_waits_for_a_login_in_progress(server, provider, stores, monkeypatch):
    # A rejected call clearing the session while another call is logging in
    # must wait for that login, never let a second one run alongside it:
    # both would rotate the same refresh token.
    import threading
    from dataclasses import replace

    started, release = threading.Event(), threading.Event()
    running, overlaps = [], []

    def slow_login(token_store, **_):
        running.append(1)
        if len(running) > 1:
            overlaps.append(True)
        started.set()
        release.wait(5)
        running.pop()
        return Session(int(token_store.parent.name))

    monkeypatch.setattr(mcp_server, "login", slow_login)
    caller = threading.Thread(target=sleep_as, args=(server, ME))
    caller.start()
    assert started.wait(5)
    exposure = replace(mcp_server.REMOTE, garmin_session=provider.garmin_session, login_url="x")

    def reset():  # what a call that Garmin rejected does, as ME
        mcp_server._exposure.set(exposure)
        mcp_server._user.set(ME)
        mcp_server._reset_api()

    resetter = threading.Thread(target=reset)
    resetter.start()
    resetter.join(0.3)
    assert resetter.is_alive()  # waiting for the login's lock
    release.set()
    caller.join(5)
    resetter.join(5)
    assert sleep_as(server, ME) == {"profile": 1001}  # logs in again, alone
    assert overlaps == []


# --- stored sessions nobody uses -------------------------------------------------------


def test_moving_to_another_account_orphans_only_unshared_profiles(provider):
    assert provider.set_garmin_profile(ME, 1001) is None
    assert provider.set_garmin_profile(ME, 1001) is None  # same account again
    assert provider.set_garmin_profile(FRIEND, 1001) is None
    assert provider.set_garmin_profile(ME, 3003) is None  # FRIEND still uses 1001
    assert provider.set_garmin_profile(FRIEND, 4004) == 1001  # now nobody does
    assert provider.garmin_profiles() == {3003, 4004}


def test_startup_deletes_stored_sessions_nobody_uses(tmp_path):
    db = tmp_path / "mcp_auth.sqlite"
    provider = GarminReaderOAuthProvider(db, PUBLIC)
    try:
        provider.set_garmin_profile(ME, 1001)
        for name in ("1001", "5555"):
            (users_dir() / name / "garmin_tokens").mkdir(parents=True)
        (users_dir() / "notes").mkdir()  # not a profile folder: left alone
        mcp_server._profile_sessions[5555] = mcp_server._ProfileSession(api=object())
        mcp_server.build_server(HttpSettings(public_url=PUBLIC), provider)
        assert sorted(p.name for p in users_dir().iterdir()) == ["1001", "notes"]
        assert 5555 not in mcp_server._profile_sessions
    finally:
        provider.close()


def test_import_deletes_the_users_previous_session(monkeypatch, tmp_path, provider):
    monkeypatch.setenv("GARMIN_READER_STATE_DIR", str(tmp_path))  # where `provider` keeps its database
    provider.set_garmin_profile(FRIEND, 2002)
    user_token_store(2002).mkdir(parents=True)
    monkeypatch.setattr(mcp_server, "login", lambda **_: Importable(3003))
    assert mcp_server.main(["--import-garmin-session", FRIEND, str(tmp_path / "import")]) == 0
    assert provider.garmin_profile(FRIEND) == 3003
    assert not user_token_store(2002).parent.exists()


def test_revision_is_read_again_once_its_the_calls_turn(server, provider, stores):
    # A call waiting for the profile's lock while a sign-in happens, and
    # another call loads the new session, must use that session, not
    # replace it again because of the revision it read before waiting.
    import threading
    from dataclasses import replace

    assert sleep_as(server, ME) == {"profile": 1001}
    entry = mcp_server._profile_sessions[1001]
    read, proceed = threading.Event(), threading.Event()

    def garmin_session(email):
        link = provider.garmin_session(email)
        if not read.is_set():
            read.set()
            proceed.wait(5)
        return link

    exposure = replace(mcp_server.REMOTE, garmin_session=garmin_session, login_url="x")
    used = []

    def call():  # what a tool call as ME does to get its session
        mcp_server._exposure.set(exposure)
        mcp_server._user.set(ME)
        used.append(mcp_server._get_api())

    entry.lock.acquire()  # another call is busy with this profile
    try:
        waiting = threading.Thread(target=call)
        waiting.start()
        assert read.wait(5)  # it has read the old revision...
        proceed.set()
        provider.set_garmin_profile(ME, 1001)  # ...then ME signs in again,
        fresh = Session(1001)  # and the busy call loads the new session
        entry.api, entry.revision = fresh, provider.garmin_session(ME)[1]
        entry.verified = True
    finally:
        entry.lock.release()
    waiting.join(5)
    assert used == [fresh]
    assert len(stores.logins) == 1  # no needless second login
