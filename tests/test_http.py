import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from garmin_reader import mcp_server
from garmin_reader.settings import HttpSettings, garmin_token_store, setting, state_dir

PUBLIC = "https://abc-def.trycloudflare.com"
HEADERS = {
    "Host": "abc-def.trycloudflare.com",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


# --- settings ---------------------------------------------------------------


def test_settings_from_env():
    s = HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": PUBLIC + "/", "GARMIN_READER_PORT": "9000"})
    assert s == HttpSettings(public_url=PUBLIC, port=9000)
    assert s.public_host == "abc-def.trycloudflare.com"


def test_settings_flags_override_env():
    s = HttpSettings.from_env(
        {"GARMIN_READER_PUBLIC_URL": "https://old.example", "GARMIN_READER_PORT": "9000"},
        public_url=PUBLIC, port="9100",
    )
    assert s == HttpSettings(public_url=PUBLIC, port=9100)


def test_settings_default_port():
    assert HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": PUBLIC}).port == 8000


def test_settings_require_public_url():
    with pytest.raises(ValueError, match="GARMIN_READER_PUBLIC_URL"):
        HttpSettings.from_env({})


@pytest.mark.parametrize(
    "url",
    [
        "http://abc.trycloudflare.com",  # plain http off localhost
        "abc.trycloudflare.com",  # no scheme
        "https://abc.trycloudflare.com/mcp",  # path
        "https://abc.trycloudflare.com?x=1",  # query
        "https://",  # no host
        "https://abc.trycloudflare.com:notaport",  # malformed port
        "https://abc.trycloudflare.com:70000",  # port out of range
    ],
)
def test_settings_reject_bad_urls(url):
    with pytest.raises(ValueError, match="GARMIN_READER_PUBLIC_URL"):
        HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": url})


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@abc.trycloudflare.com",
        "https://user@abc.trycloudflare.com",
        # Would fail other checks too, whose messages echo the URL.
        "http://user:secret@abc.trycloudflare.com",
        "https://user:secret@abc.trycloudflare.com:notaport/mcp",
    ],
)
def test_settings_reject_credentials_without_echoing_them(url):
    with pytest.raises(ValueError, match="user name or password") as err:
        HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": url})
    assert "secret" not in str(err.value) and "user@" not in str(err.value)


@pytest.mark.parametrize(
    "url", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"]
)
def test_settings_allow_http_on_localhost(url):
    assert HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": url}).public_url == url


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://abc.trycloudflare.com:443", "https://abc.trycloudflare.com"),
        ("http://localhost:80", "http://localhost"),
        ("http://[::1]:80", "http://[::1]"),
        ("https://abc.trycloudflare.com:8443", "https://abc.trycloudflare.com:8443"),
    ],
)
def test_settings_drop_default_ports(url, expected):
    # Clients omit default ports from Host/Origin, which must match exactly.
    s = HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": url})
    assert s.public_url == expected
    assert s.public_host == expected.split("://")[1]


def test_default_port_url_accepts_real_host_header():
    server = mcp_server.build_server(
        HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": PUBLIC + ":443"})
    )
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        assert post(c, INIT).status_code == 200


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_settings_reject_bad_ports(port):
    with pytest.raises(ValueError, match="GARMIN_READER_PORT"):
        HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": PUBLIC, "GARMIN_READER_PORT": port})


def test_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert state_dir({}) == tmp_path / ".garmin-reader"
    store = tmp_path / "elsewhere"
    assert garmin_token_store({"GARMIN_READER_STATE_DIR": str(store)}) == store / "garmin_tokens"


def test_state_dir_from_before_the_rename_is_still_used(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".kcal").mkdir()
    assert state_dir({}) == tmp_path / ".kcal"
    assert state_dir({"KCAL_STATE_DIR": str(tmp_path / "old")}) == tmp_path / "old"
    # Once the new folder exists, it wins, as does the new setting.
    (tmp_path / ".garmin-reader").mkdir()
    assert state_dir({}) == tmp_path / ".garmin-reader"
    env = {"GARMIN_READER_STATE_DIR": str(tmp_path / "new"), "KCAL_STATE_DIR": str(tmp_path / "old")}
    assert state_dir(env) == tmp_path / "new"


def test_settings_from_before_the_rename_are_still_read():
    assert setting("PORT", {"KCAL_PORT": "9000"}) == "9000"
    assert setting("PORT", {"KCAL_PORT": "9000", "GARMIN_READER_PORT": "9001"}) == "9001"
    assert setting("PORT", {}) is None
    s = HttpSettings.from_env({
        "KCAL_PUBLIC_URL": PUBLIC,
        "KCAL_PORT": "9000",
        "KCAL_HOST": "0.0.0.0",
        "KCAL_ALLOWED_EMAILS": "me@example.com",
    })
    assert (s.public_url, s.port, s.host, s.allowed_emails) == (
        PUBLIC, 9000, "0.0.0.0", frozenset({"me@example.com"})
    )


# --- HTTP transport ---------------------------------------------------------


@pytest.fixture
def client():
    server = mcp_server.build_server(HttpSettings(public_url=PUBLIC))
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        yield c


def post(client, body, **headers):
    return client.post("/mcp", content=json.dumps(body), headers={**HEADERS, **headers})


def test_initialize_returns_plain_json_without_session(client):
    r = post(client, INIT)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in r.headers
    assert r.json()["result"]["serverInfo"]["name"] == "garmin-reader"


def test_requests_stand_alone_without_initialize(client):
    # No MCP sessions: a fresh request works without an initialize first,
    # e.g. right after a server restart.
    r = post(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "get_garmin_summary" in names and len(names) == 5


def test_tool_call_over_http(client):
    r = post(client, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "get_garmin_reader_info", "arguments": {}},
    })
    content = r.json()["result"]["content"]
    assert len(content) == 1
    assert json.loads(content[0]["text"])["stale"] is False


def test_localhost_host_allowed(client):
    assert post(client, INIT, Host="127.0.0.1:8000").status_code == 200


def test_foreign_host_rejected(client):
    # DNS rebinding protection stays on for anything but the public host.
    assert post(client, INIT, Host="evil.example").status_code == 421


def test_public_origin_allowed(client):
    assert post(client, INIT, Origin=PUBLIC).status_code == 200


def test_foreign_origin_rejected(client):
    # MCP SDK 1.10-1.14 answer 400 here; later versions 403.
    assert post(client, INIT, Origin="https://evil.example").status_code in (400, 403)


def test_stdio_server_has_no_http_settings():
    assert mcp_server.mcp.settings.stateless_http is False


# --- entry point ------------------------------------------------------------


@pytest.fixture
def runs(monkeypatch, tmp_path):
    calls = []
    # main() opens the OAuth database under GARMIN_READER_STATE_DIR: keep it out of ~/.garmin-reader.
    monkeypatch.setenv("GARMIN_READER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        mcp_server.FastMCP, "run",
        lambda self, transport="stdio", **_: calls.append((self, transport)),
    )
    monkeypatch.setattr(
        mcp_server, "serve_http", lambda server: calls.append((server, "streamable-http"))
    )
    monkeypatch.delenv("GARMIN_READER_PUBLIC_URL", raising=False)
    monkeypatch.delenv("GARMIN_READER_PORT", raising=False)
    return calls


def test_main_defaults_to_stdio(runs):
    assert mcp_server.main([]) == 0
    assert runs == [(mcp_server.mcp, "stdio")]


def test_main_http_always_uses_oauth(runs, capsys, tmp_path):
    assert mcp_server.main(["--http", "--public-url", PUBLIC, "--allowed-emails", "me@example.com"]) == 0
    [(server, transport)] = runs
    assert transport == "streamable-http"
    assert server.settings.auth is not None
    assert str(server.settings.auth.issuer_url).rstrip("/") == PUBLIC
    assert (tmp_path / "mcp_auth.sqlite").exists()
    assert f"1 user(s) may sign in at {PUBLIC}/login" in capsys.readouterr().err


def test_main_http_has_no_unauthenticated_mode(runs):
    with pytest.raises(SystemExit):
        mcp_server.main(["--http", "--no-auth", "--public-url", PUBLIC, "--allowed-emails", "me@example.com"])
    assert runs == []


def test_main_http_requires_allowed_emails(runs, capsys):
    assert mcp_server.main(["--http", "--public-url", PUBLIC]) == 2
    assert runs == []
    assert "GARMIN_READER_ALLOWED_EMAILS" in capsys.readouterr().err


def test_main_http_rejects_entries_that_arent_emails(runs, capsys, monkeypatch):
    monkeypatch.setenv("GARMIN_READER_ALLOWED_EMAILS", "me@example.com, 2426101")
    assert mcp_server.main(["--http", "--public-url", PUBLIC]) == 2
    assert "aren't emails" in capsys.readouterr().err and runs == []


def test_main_http_explains_the_old_owner_setting(runs, capsys, monkeypatch):
    monkeypatch.setenv("KCAL_GARMIN_OWNER", "2426101")
    monkeypatch.setenv("GARMIN_READER_ALLOWED_EMAILS", "me@example.com")
    assert mcp_server.main(["--http", "--public-url", PUBLIC]) == 2
    assert "GARMIN_READER_ALLOWED_EMAILS" in capsys.readouterr().err and runs == []


def test_allowed_emails_are_lowercased_and_split():
    s = HttpSettings.from_env({
        "GARMIN_READER_PUBLIC_URL": PUBLIC,
        "GARMIN_READER_ALLOWED_EMAILS": " Me@Example.com,friend@example.com\n a@b.pl ",
    })
    assert s.allowed_emails == {"me@example.com", "friend@example.com", "a@b.pl"}


def test_allowed_emails_flag_wins_over_env():
    s = HttpSettings.from_env(
        {"GARMIN_READER_PUBLIC_URL": PUBLIC, "GARMIN_READER_ALLOWED_EMAILS": "env@example.com"},
        allowed_emails="flag@example.com",
    )
    assert s.allowed_emails == {"flag@example.com"}


def test_main_http_reports_bad_settings(runs, capsys):
    assert mcp_server.main(["--http"]) == 2
    assert runs == []
    assert "GARMIN_READER_PUBLIC_URL" in capsys.readouterr().err


def test_main_http_runs_streamable_http(runs, monkeypatch):
    monkeypatch.setenv("GARMIN_READER_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("GARMIN_READER_ALLOWED_EMAILS", "me@example.com")
    assert mcp_server.main(["--http", "--port", "8123"]) == 0
    [(server, transport)] = runs
    assert transport == "streamable-http"
    assert server.settings.port == 8123
    assert server.settings.host == "127.0.0.1"
    assert server.settings.stateless_http and server.settings.json_response


# --- rejection logging ------------------------------------------------------


@pytest.fixture
def logged_client(caplog):
    server = mcp_server.build_server(HttpSettings(public_url=PUBLIC))
    app = mcp_server.LogClientErrors(server.streamable_http_app())
    caplog.set_level("WARNING", logger="garmin_reader.http")
    with TestClient(app, base_url=PUBLIC) as c:
        yield c


def rejections(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "garmin_reader.http"]


LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def test_logs_why_a_request_was_rejected(logged_client, caplog):
    # The SDK checks the version header on every request but initialize.
    r = post(logged_client, LIST, **{"MCP-Protocol-Version": "1999-01-01"})
    assert r.status_code == 400
    [line] = rejections(caplog)
    assert line.startswith("POST /mcp -> 400: Bad Request: Unsupported protocol version: 1999-01-01")
    assert "mcp-protocol-version=1999-01-01" in line
    assert "content-type=application/json" in line


def test_logs_malformed_body(logged_client, caplog):
    r = logged_client.post("/mcp", content=b"{not json", headers=HEADERS)
    assert r.status_code == 400
    [line] = rejections(caplog)
    assert "-> 400: Parse error" in line


def test_logs_rejected_host(logged_client, caplog):
    assert post(logged_client, INIT, Host="evil.example").status_code == 421
    [line] = rejections(caplog)
    assert "-> 421: Invalid Host header" in line


def test_never_logs_credentials(logged_client, caplog):
    post(
        logged_client, LIST,
        **{"MCP-Protocol-Version": "1999-01-01", "Authorization": "Bearer s3cret",
           "Cookie": "session=s3cret"},
    )
    [line] = rejections(caplog)
    assert "s3cret" not in line and "authorization" not in line.lower()


def test_does_not_log_success_or_404(logged_client, caplog):
    assert post(logged_client, INIT).status_code == 200
    assert logged_client.get("/.well-known/oauth-protected-resource").status_code == 404
    assert rejections(caplog) == []


# --- running on a host --------------------------------------------------------


def test_host_defaults_to_loopback():
    assert HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": PUBLIC}).host == "127.0.0.1"


@pytest.mark.parametrize("value", ["0.0.0.0", "::", "10.0.0.5"])
def test_host_from_env(value):
    assert HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": PUBLIC, "GARMIN_READER_HOST": value}).host == value


def test_host_flag_wins_over_env():
    s = HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": PUBLIC, "GARMIN_READER_HOST": "0.0.0.0"}, host="::1")
    assert s.host == "::1"


@pytest.mark.parametrize("value", ["example.com", "0.0.0.0:8000", "everywhere"])
def test_host_must_be_an_ip(value):
    with pytest.raises(ValueError, match="GARMIN_READER_HOST"):
        HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": PUBLIC, "GARMIN_READER_HOST": value})


def test_platform_port_is_used_when_garmin_reader_port_isnt_set():
    # Cloud Run and similar platforms tell the server its port in $PORT.
    assert HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": PUBLIC, "PORT": "8080"}).port == 8080
    s = HttpSettings.from_env({"GARMIN_READER_PUBLIC_URL": PUBLIC, "PORT": "8080", "GARMIN_READER_PORT": "9000"})
    assert s.port == 9000


def test_main_listens_where_told(runs, monkeypatch):
    monkeypatch.setenv("GARMIN_READER_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("GARMIN_READER_ALLOWED_EMAILS", "me@example.com")
    monkeypatch.setenv("GARMIN_READER_HOST", "0.0.0.0")
    assert mcp_server.main(["--http"]) == 0
    [(server, _)] = runs
    assert server.settings.host == "0.0.0.0"


def test_healthz_needs_no_sign_in_and_says_nothing_else(tmp_path):
    from garmin_reader.oauth import GarminReaderOAuthProvider

    provider = GarminReaderOAuthProvider(tmp_path / "a.sqlite", PUBLIC)
    server = mcp_server.build_server(HttpSettings(public_url=PUBLIC, allowed_emails=frozenset({"me@example.com"})), provider)
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        assert post(c, INIT).status_code == 401  # /mcp still needs a token
        # Health checks often come from inside the platform, with any Host.
        r = c.get("/healthz", headers={"Host": "10.0.0.5:8000"})
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        assert r.headers["cache-control"] == "no-store"
        assert c.post("/healthz").status_code == 405
    provider.close()


def test_healthz_does_not_touch_garmin(client, monkeypatch):
    monkeypatch.setattr(mcp_server, "_api", None)
    monkeypatch.setattr(mcp_server, "login", lambda **_: pytest.fail("contacted Garmin"))
    assert client.get("/healthz").status_code == 200
