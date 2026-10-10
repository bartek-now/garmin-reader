import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from kcal import mcp_server
from kcal.settings import HttpSettings, garmin_token_store, state_dir

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
    s = HttpSettings.from_env({"KCAL_PUBLIC_URL": PUBLIC + "/", "KCAL_PORT": "9000"})
    assert s == HttpSettings(public_url=PUBLIC, port=9000)
    assert s.public_host == "abc-def.trycloudflare.com"


def test_settings_flags_override_env():
    s = HttpSettings.from_env(
        {"KCAL_PUBLIC_URL": "https://old.example", "KCAL_PORT": "9000"},
        public_url=PUBLIC, port="9100",
    )
    assert s == HttpSettings(public_url=PUBLIC, port=9100)


def test_settings_default_port():
    assert HttpSettings.from_env({"KCAL_PUBLIC_URL": PUBLIC}).port == 8000


def test_settings_require_public_url():
    with pytest.raises(ValueError, match="KCAL_PUBLIC_URL"):
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
    with pytest.raises(ValueError, match="KCAL_PUBLIC_URL"):
        HttpSettings.from_env({"KCAL_PUBLIC_URL": url})


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
        HttpSettings.from_env({"KCAL_PUBLIC_URL": url})
    assert "secret" not in str(err.value) and "user@" not in str(err.value)


@pytest.mark.parametrize(
    "url", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"]
)
def test_settings_allow_http_on_localhost(url):
    assert HttpSettings.from_env({"KCAL_PUBLIC_URL": url}).public_url == url


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
    s = HttpSettings.from_env({"KCAL_PUBLIC_URL": url})
    assert s.public_url == expected
    assert s.public_host == expected.split("://")[1]


def test_default_port_url_accepts_real_host_header():
    server = mcp_server.build_server(
        HttpSettings.from_env({"KCAL_PUBLIC_URL": PUBLIC + ":443"})
    )
    with TestClient(server.streamable_http_app(), base_url=PUBLIC) as c:
        assert post(c, INIT).status_code == 200


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_settings_reject_bad_ports(port):
    with pytest.raises(ValueError, match="KCAL_PORT"):
        HttpSettings.from_env({"KCAL_PUBLIC_URL": PUBLIC, "KCAL_PORT": port})


def test_state_dir(tmp_path):
    assert state_dir({}) == Path.home() / ".kcal"
    assert garmin_token_store({"KCAL_STATE_DIR": str(tmp_path)}) == tmp_path / "garmin_tokens"


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
    assert r.json()["result"]["serverInfo"]["name"] == "kcal"


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
        "params": {"name": "get_kcal_server_info", "arguments": {}},
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
    # main() opens the OAuth database under KCAL_STATE_DIR: keep it out of ~/.kcal.
    monkeypatch.setenv("KCAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        mcp_server.FastMCP, "run",
        lambda self, transport="stdio", **_: calls.append((self, transport)),
    )
    monkeypatch.setattr(
        mcp_server, "serve_http", lambda server: calls.append((server, "streamable-http"))
    )
    monkeypatch.delenv("KCAL_PUBLIC_URL", raising=False)
    monkeypatch.delenv("KCAL_PORT", raising=False)
    monkeypatch.delenv("KCAL_GARMIN_OWNER", raising=False)
    return calls


def test_main_defaults_to_stdio(runs):
    assert mcp_server.main([]) == 0
    assert runs == [(mcp_server.mcp, "stdio")]


def test_main_http_always_uses_oauth(runs, capsys, tmp_path):
    assert mcp_server.main(["--http", "--public-url", PUBLIC, "--garmin-owner", "42"]) == 0
    [(server, transport)] = runs
    assert transport == "streamable-http"
    assert server.settings.auth is not None
    assert str(server.settings.auth.issuer_url).rstrip("/") == PUBLIC
    assert (tmp_path / "mcp_auth.sqlite").exists()
    assert f"clients sign in at {PUBLIC}/login" in capsys.readouterr().err


def test_main_http_has_no_unauthenticated_mode(runs):
    with pytest.raises(SystemExit):
        mcp_server.main(["--http", "--no-auth", "--public-url", PUBLIC, "--garmin-owner", "42"])
    assert runs == []


def test_main_http_requires_owner(runs, capsys):
    assert mcp_server.main(["--http", "--public-url", PUBLIC]) == 2
    assert runs == []
    err = capsys.readouterr().err
    assert "KCAL_GARMIN_OWNER" in err and "kcal whoami" in err


def test_main_http_rejects_non_numeric_owner(runs, capsys, monkeypatch):
    monkeypatch.setenv("KCAL_GARMIN_OWNER", "bartek")
    assert mcp_server.main(["--http", "--public-url", PUBLIC]) == 2
    assert "profile ID" in capsys.readouterr().err


def test_main_http_reports_bad_settings(runs, capsys):
    assert mcp_server.main(["--http"]) == 2
    assert runs == []
    assert "KCAL_PUBLIC_URL" in capsys.readouterr().err


def test_main_http_runs_streamable_http(runs, monkeypatch):
    monkeypatch.setenv("KCAL_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("KCAL_GARMIN_OWNER", "42")
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
    caplog.set_level("WARNING", logger="kcal.http")
    with TestClient(app, base_url=PUBLIC) as c:
        yield c


def rejections(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "kcal.http"]


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
