"""The OAuth flow through the MCP SDK's real endpoints (design section 2)."""

import base64
import hashlib
import json
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from garmin_reader import mcp_server, oauth
from garmin_reader.oauth import GarminReaderOAuthProvider
from garmin_reader.settings import HttpSettings

PUBLIC = "https://abc-def.trycloudflare.com"
RESOURCE = PUBLIC + "/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
USER = "me@example.com"
OTHER = "friend@example.com"
USERS = frozenset({USER, OTHER})
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "mcp_auth.sqlite"


@pytest.fixture
def provider(db):
    p = GarminReaderOAuthProvider(db, PUBLIC)
    yield p
    p.close()


@pytest.fixture
def client(provider):
    server = mcp_server.build_server(HttpSettings(public_url=PUBLIC, allowed_emails=USERS), provider)
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        yield c


def pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def register(client):
    r = client.post("/register", json={
        "client_name": "Claude",
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def start_authorize(client, client_id, challenge, **extra):
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "st4te", "resource": RESOURCE, **extra,
    }
    return client.get("/authorize", params=params, follow_redirects=False)


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def exchange_code(client, client_id, code, verifier):
    return client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier, "resource": RESOURCE,
    })


def connect(client, provider, user=USER):
    """Register, authorize, 'log in' as `user`, exchange the code."""
    client_id = register(client)
    verifier, challenge = pkce()
    r = start_authorize(client, client_id, challenge)
    request_id = query(r.headers["location"])["req"]
    back = provider.complete_authorization(request_id, subject=user)
    r = exchange_code(client, client_id, query(back)["code"], verifier)
    assert r.status_code == 200, r.text
    return client_id, r.json()


def mcp_call(client, token):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", content=json.dumps(INIT), headers=headers)


# --- discovery -----------------------------------------------------------------


def test_authorization_server_metadata(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"].rstrip("/") == PUBLIC
    assert meta["authorization_endpoint"] == PUBLIC + "/authorize"
    assert meta["token_endpoint"] == PUBLIC + "/token"
    assert meta["registration_endpoint"] == PUBLIC + "/register"
    assert meta["revocation_endpoint"] == PUBLIC + "/revoke"
    assert meta["scopes_supported"] == [oauth.SCOPE]
    assert "S256" in meta["code_challenge_methods_supported"]


def test_protected_resource_metadata(client):
    meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == RESOURCE
    assert [s.rstrip("/") for s in meta["authorization_servers"]] == [PUBLIC]


def test_mcp_needs_a_token(client):
    r = mcp_call(client, None)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in r.headers["www-authenticate"]


def test_mcp_rejects_unknown_token(client):
    assert mcp_call(client, "not-a-token").status_code == 401


# --- the full flow ---------------------------------------------------------------


def test_authorize_sends_browser_to_login(client, provider):
    client_id = register(client)
    r = start_authorize(client, client_id, pkce()[1])
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith(PUBLIC + "/login?req=")
    pending = provider.pending(query(location)["req"])
    assert pending.client.client_name == "Claude"
    assert str(pending.params.redirect_uri) == REDIRECT


def test_full_flow_then_mcp_works(client, provider):
    _, tokens = connect(client, provider)
    assert tokens["token_type"].lower() == "bearer"
    assert tokens["expires_in"] == oauth.ACCESS_TOKEN_SECONDS
    assert tokens["scope"] == oauth.SCOPE
    r = mcp_call(client, tokens["access_token"])
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["name"] == "garmin-reader"


def test_completion_redirects_back_with_state(client, provider):
    client_id = register(client)
    r = start_authorize(client, client_id, pkce()[1])
    back = provider.complete_authorization(query(r.headers["location"])["req"], USER)
    assert back.startswith(REDIRECT + "?")
    assert query(back)["state"] == "st4te" and query(back)["code"]


def test_denied_authorization_redirects_with_error(client, provider):
    client_id = register(client)
    r = start_authorize(client, client_id, pkce()[1])
    back = provider.deny_authorization(query(r.headers["location"])["req"])
    assert query(back) == {"error": "access_denied", "state": "st4te"}


def test_authorization_request_is_single_use(client, provider):
    client_id = register(client)
    request_id = query(start_authorize(client, client_id, pkce()[1]).headers["location"])["req"]
    provider.complete_authorization(request_id, USER)
    with pytest.raises(KeyError):
        provider.complete_authorization(request_id, USER)
    assert provider.deny_authorization(request_id) is None


def test_authorization_request_expires(client, provider, monkeypatch):
    client_id = register(client)
    request_id = query(start_authorize(client, client_id, pkce()[1]).headers["location"])["req"]
    later = time.time() + oauth.PENDING_SECONDS + 1
    monkeypatch.setattr(oauth.time, "time", lambda: later)
    assert provider.pending(request_id) is None
    with pytest.raises(KeyError):
        provider.complete_authorization(request_id, USER)


def test_authorize_rejects_other_resource(client):
    client_id = register(client)
    r = start_authorize(client, client_id, pkce()[1], resource="https://evil.example/mcp")
    assert r.status_code == 302
    assert query(r.headers["location"])["error"] == "invalid_request"
    assert r.headers["location"].startswith(REDIRECT)


# --- code exchange ---------------------------------------------------------------


def authorized_code(client, provider):
    client_id = register(client)
    verifier, challenge = pkce()
    request_id = query(start_authorize(client, client_id, challenge).headers["location"])["req"]
    code = query(provider.complete_authorization(request_id, USER))["code"]
    return client_id, code, verifier


def test_code_is_single_use(client, provider):
    client_id, code, verifier = authorized_code(client, provider)
    assert exchange_code(client, client_id, code, verifier).status_code == 200
    r = exchange_code(client, client_id, code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_needs_the_right_verifier(client, provider):
    client_id, code, _ = authorized_code(client, provider)
    r = exchange_code(client, client_id, code, pkce()[0])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_belongs_to_its_client(client, provider):
    _, code, verifier = authorized_code(client, provider)
    other = register(client)
    r = exchange_code(client, other, code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_expires(client, provider, monkeypatch):
    client_id, code, verifier = authorized_code(client, provider)
    later = time.time() + oauth.CODE_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    r = exchange_code(client, client_id, code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


# --- refresh, expiry, revocation ---------------------------------------------------


def refresh(client, client_id, refresh_token):
    return client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "resource": RESOURCE,
    })


def test_refresh_rotates_both_tokens(client, provider):
    client_id, old = connect(client, provider)
    r = refresh(client, client_id, old["refresh_token"])
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["access_token"] != old["access_token"]
    assert new["refresh_token"] != old["refresh_token"]
    assert mcp_call(client, new["access_token"]).status_code == 200
    # The old pair is dead.
    assert mcp_call(client, old["access_token"]).status_code == 401
    r = refresh(client, client_id, old["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_refresh_token_belongs_to_its_client(client, provider):
    _, tokens = connect(client, provider)
    other = register(client)
    r = refresh(client, other, tokens["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_access_token_expires(client, provider, monkeypatch):
    _, tokens = connect(client, provider)
    later = time.time() + oauth.ACCESS_TOKEN_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert mcp_call(client, tokens["access_token"]).status_code == 401


def test_refresh_token_expires(client, provider, monkeypatch):
    client_id, tokens = connect(client, provider)
    later = time.time() + oauth.REFRESH_TOKEN_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    r = refresh(client, client_id, tokens["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


@pytest.mark.parametrize("which", ["access_token", "refresh_token"])
def test_revoking_either_token_revokes_both(client, provider, which):
    client_id, tokens = connect(client, provider)
    # The SDK's /revoke form requires the field even for public clients.
    r = client.post("/revoke", data={"token": tokens[which], "client_id": client_id, "client_secret": ""})
    assert r.status_code == 200
    assert mcp_call(client, tokens["access_token"]).status_code == 401
    assert refresh(client, client_id, tokens["refresh_token"]).status_code == 400


def test_revoke_all_signs_everyone_out(client, provider):
    _, a = connect(client, provider)
    _, b = connect(client, provider)
    provider.revoke_all()
    assert mcp_call(client, a["access_token"]).status_code == 401
    assert mcp_call(client, b["access_token"]).status_code == 401


def test_token_for_another_resource_is_refused(client, provider, db):
    _, tokens = connect(client, provider)
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE tokens SET resource = 'https://other.example/mcp'")
    assert mcp_call(client, tokens["access_token"]).status_code == 401


# --- storage -----------------------------------------------------------------------


def test_clients_and_tokens_survive_a_restart(client, provider, db):
    client_id, tokens = connect(client, provider)
    provider.close()
    reopened = GarminReaderOAuthProvider(db, PUBLIC)
    server = mcp_server.build_server(HttpSettings(public_url=PUBLIC, allowed_emails=USERS), reopened)
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        assert mcp_call(c, tokens["access_token"]).status_code == 200
        assert refresh(c, client_id, tokens["refresh_token"]).status_code == 200
    reopened.close()


def test_tokens_are_stored_hashed(client, provider, db):
    _, tokens = connect(client, provider)
    raw = db.read_bytes()
    assert tokens["access_token"].encode() not in raw
    assert tokens["refresh_token"].encode() not in raw


def test_tokens_carry_their_user(client, provider):
    import asyncio
    _, mine = connect(client, provider)
    _, theirs = connect(client, provider, user=OTHER)
    loaded = asyncio.run(provider.load_access_token(mine["access_token"]))
    assert loaded.subject == USER and loaded.resource == RESOURCE
    loaded = asyncio.run(provider.load_access_token(theirs["access_token"]))
    assert loaded.subject == OTHER


# --- the login page route ------------------------------------------------------------


def test_login_page_is_served(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


def test_provider_hides_tokens_from_other_clients(client, provider):
    # The SDK checks this too; the provider shouldn't rely on it.
    import asyncio
    client_id, tokens = connect(client, provider)
    other = asyncio.run(provider.get_client(register(client)))
    mine = asyncio.run(provider.get_client(client_id))
    assert asyncio.run(provider.load_refresh_token(other, tokens["refresh_token"])) is None
    assert asyncio.run(provider.load_refresh_token(mine, tokens["refresh_token"])) is not None


# --- review fixes -------------------------------------------------------------------


def test_replayed_refresh_token_ends_the_whole_chain(client, provider):
    client_id, first = connect(client, provider)
    second = refresh(client, client_id, first["refresh_token"]).json()
    # The first refresh token comes back (stolen, or replayed): refused, and
    # the tokens issued from it stop working too.
    r = refresh(client, client_id, first["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert mcp_call(client, second["access_token"]).status_code == 401
    assert refresh(client, client_id, second["refresh_token"]).status_code == 400


def test_chain_survives_several_rotations(client, provider):
    client_id, tokens = connect(client, provider)
    for _ in range(3):
        tokens = refresh(client, client_id, tokens["refresh_token"]).json()
    assert mcp_call(client, tokens["access_token"]).status_code == 200
    r = client.post("/revoke", data={
        "token": tokens["refresh_token"], "client_id": client_id, "client_secret": "",
    })
    assert r.status_code == 200
    assert mcp_call(client, tokens["access_token"]).status_code == 401


def test_code_single_use_enforced_by_provider(client, provider):
    import asyncio
    client_id, code, _ = authorized_code(client, provider)
    c = asyncio.run(provider.get_client(client_id))
    loaded = asyncio.run(provider.load_authorization_code(c, code))
    asyncio.run(provider.exchange_authorization_code(c, loaded))
    with pytest.raises(oauth.TokenError):
        asyncio.run(provider.exchange_authorization_code(c, loaded))


def test_pending_requests_are_capped(client, provider, monkeypatch):
    monkeypatch.setattr(oauth, "MAX_PENDING", 3)
    client_id = register(client)
    ids = [
        query(start_authorize(client, client_id, pkce()[1]).headers["location"])["req"]
        for _ in range(5)
    ]
    assert len(provider._pending) == 3
    assert provider.pending(ids[0]) is None and provider.pending(ids[-1]) is not None


def test_unused_clients_are_forgotten_after_a_day(client, provider, monkeypatch):
    import asyncio
    used, _ = connect(client, provider)
    unused = register(client)
    later = time.time() + oauth.UNUSED_CLIENT_SECONDS + 1
    monkeypatch.setattr(oauth.time, "time", lambda: later)
    register(client)  # registration prunes
    assert asyncio.run(provider.get_client(unused)) is None
    assert asyncio.run(provider.get_client(used)) is not None


def test_registration_stops_at_the_limit(client, monkeypatch):
    monkeypatch.setattr(oauth, "MAX_CLIENTS", 2)
    register(client)
    register(client)
    r = client.post("/register", json={
        "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


def test_resource_matches_regardless_of_host_case(client, provider):
    client_id = register(client)
    r = start_authorize(client, client_id, pkce()[1], resource="HTTPS://ABC-DEF.trycloudflare.COM/mcp/")
    assert r.headers["location"].startswith(PUBLIC + "/login?req=")


def test_public_url_host_is_lowercased():
    s = HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": "https://Abc-DEF.trycloudflare.com"})
    assert s.public_url == PUBLIC


def test_deny_ignores_expired_request(client, provider, monkeypatch):
    client_id = register(client)
    request_id = query(start_authorize(client, client_id, pkce()[1]).headers["location"])["req"]
    later = time.time() + oauth.PENDING_SECONDS + 1
    monkeypatch.setattr(oauth.time, "time", lambda: later)
    assert provider.deny_authorization(request_id) is None


def test_oauth_needs_http_settings(provider):
    with pytest.raises(ValueError, match="needs HTTP settings"):
        mcp_server.build_server(HttpSettings(public_url=PUBLIC), provider)
    with pytest.raises(ValueError, match="needs HTTP settings"):
        mcp_server.build_server(oauth=provider)


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permissions")
def test_state_is_owner_only(tmp_path):
    import os
    db = tmp_path / "state" / "mcp_auth.sqlite"
    GarminReaderOAuthProvider(db, PUBLIC).close()
    assert os.stat(db.parent).st_mode & 0o077 == 0
    assert os.stat(db).st_mode & 0o077 == 0


def test_revoke_all_command(client, provider, db, monkeypatch, capsys):
    _, tokens = connect(client, provider)
    monkeypatch.setenv("GARMIN_READER_STATE_DIR", str(db.parent))
    assert mcp_server.main(["--revoke-all"]) == 0
    assert "signed out 1 client authorization" in capsys.readouterr().err
    assert mcp_call(client, tokens["access_token"]).status_code == 401


def test_revoke_all_command_without_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GARMIN_READER_STATE_DIR", str(tmp_path))
    assert mcp_server.main(["--revoke-all"]) == 0
    assert "nothing to revoke" in capsys.readouterr().err
    assert not (tmp_path / "mcp_auth.sqlite").exists()


def test_removed_user_loses_tokens_and_profile(client, provider, db):
    client_id, mine = connect(client, provider)
    _, theirs = connect(client, provider, user=OTHER)
    provider.set_garmin_profile(USER, 1)
    provider.set_garmin_profile(OTHER, 2)
    provider.close()
    restarted = GarminReaderOAuthProvider(db, PUBLIC)
    try:
        assert restarted.configure_allowed(frozenset({OTHER})) == [1]
        server = mcp_server.build_server(
            HttpSettings(public_url=PUBLIC, allowed_emails=frozenset({OTHER})), restarted
        )
        with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
            assert mcp_call(c, mine["access_token"]).status_code == 401
            assert refresh(c, client_id, mine["refresh_token"]).status_code == 400
            assert mcp_call(c, theirs["access_token"]).status_code == 200
        assert restarted.garmin_profile(USER) is None
        assert restarted.garmin_profile(OTHER) == 2
        with sqlite3.connect(db) as raw:
            subjects = {s for (s,) in raw.execute("SELECT subject FROM tokens")}
            assert subjects == {OTHER}
            assert raw.execute("SELECT email FROM users").fetchall() == [(OTHER,)]
        # Allowed again later: they connect again, from scratch.
        assert restarted.configure_allowed(USERS) == []
        assert restarted.garmin_profile(USER) is None
    finally:
        restarted.close()


def test_token_of_a_user_who_isnt_allowed_never_loads(client, provider, db):
    # Still in the database, e.g. restored from a backup: refused anyway.
    client_id, tokens = connect(client, provider, user=OTHER)
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE tokens SET subject = 'stranger@example.com'")
    assert mcp_call(client, tokens["access_token"]).status_code == 401
    assert refresh(client, client_id, tokens["refresh_token"]).status_code == 400


def test_profile_shared_by_an_allowed_user_is_not_orphaned(provider):
    provider.configure_allowed(USERS)
    provider.set_garmin_profile(USER, 7)
    provider.set_garmin_profile(OTHER, 7)  # e.g. the account's login email changed
    assert provider.configure_allowed(frozenset({OTHER})) == []


def test_tokens_from_before_multiple_users_are_dropped(client, provider, db):
    _, mine = connect(client, provider)
    _, old = connect(client, provider)
    with sqlite3.connect(db) as raw:
        # Single-owner servers stored the Garmin profile ID as the subject.
        raw.execute("UPDATE tokens SET subject = '12345' WHERE token_hash = ?",
                    (oauth._hash(old["access_token"]),))
        raw.execute("UPDATE auth_state SET owner = '12345'")
    migrated = GarminReaderOAuthProvider(db, PUBLIC)
    try:
        server = mcp_server.build_server(HttpSettings(public_url=PUBLIC, allowed_emails=USERS), migrated)
        with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
            assert mcp_call(c, mine["access_token"]).status_code == 200
            assert mcp_call(c, old["access_token"]).status_code == 401
        with sqlite3.connect(db) as raw:
            assert raw.execute(
                "SELECT COUNT(*) FROM tokens WHERE subject = '12345'"
            ).fetchone()[0] == 0
    finally:
        migrated.close()


def test_provider_rejects_users_who_arent_allowed(client, provider):
    client_id = register(client)
    req = query(start_authorize(client, client_id, pkce()[1]).headers["location"])["req"]
    with pytest.raises(KeyError, match="Not an allowed user"):
        provider.complete_authorization(req, "stranger@example.com")
    with pytest.raises(KeyError, match="Not an allowed user"):
        provider.set_garmin_profile("stranger@example.com", 3)
    assert provider.garmin_profile("stranger@example.com") is None
    # The request still waits for an allowed user.
    assert provider.pending(req) is not None


def test_unconfigured_provider_allows_nobody(db):
    p = GarminReaderOAuthProvider(db, PUBLIC)
    try:
        assert not p.is_allowed(USER)
        assert p.garmin_profile(USER) is None
    finally:
        p.close()


def test_revoke_cli_invalidates_codes_and_waiting_logins(client, provider, db, monkeypatch):
    client_id, code, verifier = authorized_code(client, provider)
    req = query(start_authorize(client, client_id, pkce()[1]).headers["location"])["req"]
    monkeypatch.setenv("GARMIN_READER_STATE_DIR", str(db.parent))
    assert mcp_server.main(["--revoke-all"]) == 0
    r = exchange_code(client, client_id, code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert provider.pending(req) is None
    with pytest.raises(KeyError):
        provider.complete_authorization(req, USER)
    # A genuinely new authorization after revocation remains usable.
    _, tokens = connect(client, provider)
    assert mcp_call(client, tokens["access_token"]).status_code == 200


@pytest.mark.parametrize("kind", ["code", "refresh"])
def test_revocation_between_loading_and_exchanging(client, provider, db, kind):
    import asyncio

    if kind == "code":
        client_id, code, _ = authorized_code(client, provider)
        registered = asyncio.run(provider.get_client(client_id))
        loaded = asyncio.run(provider.load_authorization_code(registered, code))
        exchange = lambda: provider.exchange_authorization_code(registered, loaded)
    else:
        client_id, tokens = connect(client, provider)
        registered = asyncio.run(provider.get_client(client_id))
        loaded = asyncio.run(provider.load_refresh_token(registered, tokens["refresh_token"]))
        exchange = lambda: provider.exchange_refresh_token(registered, loaded, [oauth.SCOPE])
    assert loaded is not None
    other_process = GarminReaderOAuthProvider(db, PUBLIC)
    try:
        other_process.revoke_all()
        with pytest.raises(oauth.TokenError):
            asyncio.run(exchange())
        with sqlite3.connect(db) as raw:
            assert raw.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0
    finally:
        other_process.close()
