"""MCP server exposing Garmin daily steps/calories/weight as a tool.

Runs over stdio by default, or over Streamable HTTP with `--http` (see
docs/http-server-design.md). Over stdio, login is non-interactive: run
`garmin-reader fetch` once in a terminal first so the session token (and any MFA
code) is cached under GARMIN_READER_STATE_DIR (default ~/.garmin-reader). Over HTTP, each
allowed user signs in to Garmin on the /login page and gets their own
session.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.metadata
import inspect
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import anyio
import uvicorn
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from garmin_reader import __version__, endpoints, summary
from garmin_reader.auth import GarminLoginError, garmin_profile_id, is_refresh_unavailable, login
from garmin_reader.daily import resolve_days
from garmin_reader.http_log import LogClientErrors
from garmin_reader.login_page import GarminGateway, LoginPage
from garmin_reader.oauth import GarminReaderOAuthProvider, auth_settings
from garmin_reader.settings import (
    HttpSettings,
    normalize_email,
    parse_allowed_emails,
    state_dir,
    user_token_store,
    users_dir,
)

# Filled by @_tool; build_server() registers them on each FastMCP instance.
_TOOLS = []


@dataclass(frozen=True)
class _Exposure:
    """What a server's tools may reach and how their results are filtered."""

    allowed: frozenset[str] | None  # endpoint names without `get_`; None = all
    scrub: Callable[[Any], Any]
    # Where users refresh an expired Garmin session (HTTP with OAuth).
    login_url: str | None = None
    # HTTP with OAuth: each call uses the Garmin session of the user its
    # token belongs to; this gives that user's Garmin profile and the
    # revision of its stored session (None if they never signed in to
    # Garmin or aren't allowed any more).
    garmin_session: Callable[[str], tuple[int, str] | None] | None = None


LOCAL = _Exposure(allowed=None, scrub=lambda result: result)
REMOTE = _Exposure(allowed=endpoints.REMOTE_ALLOWED, scrub=endpoints.strip_private)
# Fails closed: code running outside a tool wrapper gets the remote rules.
# stdio tools unlock LOCAL explicitly in _wrap().
_exposure: ContextVar[_Exposure] = ContextVar("garmin_reader_exposure", default=REMOTE)
# The signed-in user (lowercase email) a tool call runs for, from its token.
_user: ContextVar[str | None] = ContextVar("garmin_reader_user", default=None)

_SRC = Path(__file__).resolve().parent


def _code_hash() -> str:
    """Short hash of the package's .py files as they are on disk right now.
    Line endings are normalized, so a Windows checkout (CRLF) and a Linux
    one (LF) of the same commit hash the same.
    """
    h = hashlib.sha256()
    for f in sorted(_SRC.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def _git_commit() -> str | None:
    """The commit of the git checkout the code runs from (with -dirty for
    uncommitted changes), else GARMIN_READER_GIT_COMMIT, which the Docker image
    records at build time since it has no git history.
    """
    try:
        return subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=_SRC, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("GARMIN_READER_GIT_COMMIT") or None


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


_LOADED_HASH = _code_hash()
_LOADED_COMMIT = _git_commit()
_STARTED = datetime.now().astimezone().isoformat(timespec="seconds")

# The one Garmin session of the local (stdio) server and of HTTP servers
# without sign-in (tests).
_api = None
_api_lock = threading.Lock()


@dataclass
class _ProfileSession:
    """One Garmin profile's loaded session (HTTP with OAuth). Kept per
    profile, not per email: two emails of one account (its login email
    changed) must share it, or each would rotate the other's refresh token.
    """

    api: Any = None
    revision: str | None = None  # of the stored session `api` was loaded from
    verified: bool = False  # `api` checked to be this profile's
    # Tools run in worker threads: concurrent first calls mustn't both log in.
    lock: threading.Lock = field(default_factory=threading.Lock)


_profile_sessions: dict[int, _ProfileSession] = {}
_profile_sessions_lock = threading.Lock()


def _get_api():
    """The Garmin session for the current call, logged in once and reused."""
    exposure = _exposure.get()
    if exposure.garmin_session is not None:
        return _user_api(exposure)
    global _api
    with _api_lock:
        if _api is None:
            try:
                _api = login(prompt_mfa=_no_mfa)
            except Exception as err:
                raise _login_error(err) from err
        return _api


def _user_api(exposure: _Exposure):
    """The current user's own Garmin session, never anyone else's: it's
    loaded from their profile's token store, reloaded whenever that store
    was replaced (a sign-in or import, in any process), and checked to be
    that profile before its first use.
    """
    user = _user.get()
    if user is None:
        # Fails closed: a call with no user never falls back to a session.
        raise GarminLoginError("This request isn't signed in to garmin-reader.")
    while True:
        link = exposure.garmin_session(user)
        if link is None:
            raise GarminLoginError(garmin_expired_message(exposure.login_url))
        with _profile_sessions_lock:
            session = _profile_sessions.setdefault(link[0], _ProfileSession())
        with session.lock:
            # Again now that it's this call's turn: a sign-in, or another
            # call reloading, may have happened while it waited.
            current = exposure.garmin_session(user)
            if current is None:
                raise GarminLoginError(garmin_expired_message(exposure.login_url))
            if current[0] == link[0]:
                return _load_session(exposure, session, *current)
        # The user moved to another Garmin account meanwhile: that one's lock.


def _load_session(exposure: _Exposure, session: _ProfileSession, profile: int, revision: str):
    """`session`'s client for the stored session at `revision`, loaded and
    checked if needed. Called with session.lock held.
    """
    if session.api is None or session.revision != revision:
        # The old client mustn't keep running: its refreshes would
        # overwrite the newly stored tokens.
        session.api, session.revision, session.verified = None, None, False
        try:
            api = login(
                token_store=user_token_store(profile), prompt_mfa=_no_mfa,
                env_credentials=False,
            )
        except Exception as err:
            raise _login_error(err) from err
        session.api, session.revision = api, revision
    if not session.verified:
        try:
            actual = garmin_profile_id(session.api)
        except GarminConnectAuthenticationError as err:
            session.api = None
            raise _login_error(err) from err
        except Exception as err:
            raise _login_error(err) from err
        if actual != profile:
            session.api = None
            raise GarminLoginError(
                "The stored Garmin session belongs to a different account. "
                f"Open {exposure.login_url} and sign in to Garmin again."
            )
        session.verified = True
    return session.api


def _remove_unused_sessions(in_use: set[int]) -> None:
    """Delete stored Garmin sessions no allowed user has: users who were
    removed, an email that moved to another Garmin account, leftovers.
    """
    folder = users_dir()
    if not folder.is_dir():
        return
    for entry in folder.iterdir():
        if entry.is_dir() and entry.name.isdigit() and int(entry.name) not in in_use:
            shutil.rmtree(entry, ignore_errors=True)
            with _profile_sessions_lock:
                _profile_sessions.pop(int(entry.name), None)


def _reset_api() -> None:
    """Forget the current call's cached Garmin session (Garmin rejected it);
    the next call logs in again.
    """
    exposure, user = _exposure.get(), _user.get()
    if exposure.garmin_session is not None:
        link = exposure.garmin_session(user) if user is not None else None
        with _profile_sessions_lock:
            session = _profile_sessions.get(link[0]) if link is not None else None
        if session is not None:
            # Cleared under the profile's own lock, which stays: replacing
            # the entry would let a second login run alongside one in
            # progress, both rotating the same refresh token.
            with session.lock:
                session.api, session.revision, session.verified = None, None, False
        return
    global _api
    with _api_lock:
        _api = None


def _login_error(err: Exception) -> GarminLoginError:
    url = _exposure.get().login_url
    if url is None:
        return GarminLoginError(str(err))
    if isinstance(err, (GarminConnectConnectionError, GarminConnectTooManyRequestsError)):
        # Garmin unreachable or limiting requests: passes on its own, and
        # sending the user to sign in would only spend sign-in attempts.
        return GarminLoginError(GARMIN_BUSY_MESSAGE)
    return GarminLoginError(garmin_expired_message(url))


GARMIN_BUSY_MESSAGE = (
    "Garmin is unreachable or limiting requests right now, so garmin-reader can't read "
    "Garmin data. This usually passes on its own: try again in a few minutes. "
    "There's no need to sign in again or reconnect the connector."
)


def garmin_expired_message(login_url: str) -> str:
    """The only cue the user gets, read by a model that knows nothing about
    garmin-reader, so it says everything (design section 3).
    """
    return (
        "Garmin session expired, so garmin-reader can't read Garmin data right now. "
        "Nothing is wrong with the connector. "
        f"To fix it, open {login_url} and sign in to Garmin (enter the "
        "verification code if Garmin asks for one). "
        "Then ask again; there's no need to reconnect the connector. "
        "Assistant: show this link to the user and wait for them to sign in; "
        "retrying before that will fail the same way."
    )


class _Garmin:
    """What tools use instead of the Garmin API: the one place the current
    exposure's allowlist is enforced, for every tool. Logs in on first use,
    so a rejected call makes no Garmin request, not even a login.
    """

    def __getattr__(self, name):
        allowed = _exposure.get().allowed
        if allowed is not None and not (
            name.startswith("get_") and name.removeprefix("get_") in allowed
        ):
            raise ValueError(f"Garmin {name!r} is not available remotely.")
        return getattr(_get_api(), name)


def _no_mfa() -> str:
    raise RuntimeError(
        "Garmin login needs an MFA code or fresh credentials. Run `garmin-reader fetch` "
        "in a terminal once to refresh the cached session, then retry."
    )


def _tool(fn):
    """Mark `fn` as a tool; build_server() registers it via _wrap()."""
    _TOOLS.append(fn)
    return fn


def _wrap(fn, exposure: _Exposure):
    """The tool as registered: its result goes out as one compact JSON text
    block, replaced by a notice if it's too large. (FastMCP's default
    pretty-prints, sends each list item as its own block, and for typed returns
    sends everything a second time as structured content.) `fn` runs in a
    worker thread: FastMCP calls sync tools on its event loop, so a slow
    Garmin request would otherwise stall every other request. While it runs,
    `_exposure` holds the server's policy, and the result is scrubbed with it.
    """

    def run(user, kwargs):
        token = _exposure.set(exposure)
        user_token = _user.set(user)
        try:
            result = exposure.scrub(fn(**kwargs))
        except GarminConnectAuthenticationError as err:
            # Garmin rejected the session mid-call (expired or revoked).
            _reset_api()
            raise _login_error(err) from err
        except (GarminConnectTooManyRequestsError, GarminConnectConnectionError) as err:
            if isinstance(err, GarminConnectConnectionError) and not is_refresh_unavailable(err):
                raise
            # A temporary refresh failure doesn't invalidate the session.
            raise RuntimeError(GARMIN_BUSY_MESSAGE) from err
        finally:
            _user.reset(user_token)
            _exposure.reset(token)
        return endpoints.to_json(endpoints.cap_size(result))

    @functools.wraps(fn)
    async def wrapper(**kwargs):
        # The user the request's OAuth token belongs to (None without sign-in).
        access = get_access_token()
        user = access.subject if access is not None else None
        return await anyio.to_thread.run_sync(run, user, kwargs)

    return wrapper


@_tool
def get_garmin_summary(
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    metrics: list[summary.Group] | None = None,
) -> dict:
    groups = list(metrics) if metrics else list(summary.DEFAULT_GROUPS)
    days = resolve_days(date, from_date, to_date, max_days=summary.MAX_RANGE_DAYS)
    return summary.summarize(_Garmin(), days, groups)


get_garmin_summary.__doc__ = """One compact row per day across a date range, for the metric groups you pick.

Use this for trends and for comparing metrics across days (e.g. HRV against
sleep, training load or logged behaviours). Dates are YYYY-MM-DD: `date` for
one day, or `from_date` (and optionally `to_date`, default yesterday) for an
inclusive range; no arguments means yesterday. `metrics` picks the groups
(default: {default}):
{groups}

The result is a table: `fields` names the columns once, and `days` has one
array per day (oldest first) in that order, null where Garmin reported
nothing. Columns empty on every day, and days with no data, are left out.
If a group can't be fetched, the others are still returned, with
`errors` saying which group failed and why. Ranges: up to {max_range} days, or
{max_per_day} when a per-day group (activity, heart, stress, lifestyle) is
included; per-day groups take about a second per 4 days. A year of up to three
range groups (e.g. hrv, sleep, readiness) fits the result size limit; with more
groups or workouts, use shorter ranges. Sleep, HRV and readiness on date D describe the night
ending on the morning of D, so they reflect day D-1's training, meals and
logged behaviours. Calories are kcal. For one day's full Garmin detail use
get_garmin_day.
""".format(
    default=", ".join(summary.DEFAULT_GROUPS),
    groups=summary.describe_groups(),
    max_range=summary.MAX_RANGE_DAYS,
    max_per_day=summary.MAX_PER_DAY_DAYS,
)


@_tool
def list_garmin_endpoints() -> list[dict]:
    """List every read-only Garmin Connect endpoint with its parameters.

    Use the names with `call_garmin_endpoint`. Dates are YYYY-MM-DD strings.
    Over a remote connection, only health and fitness endpoints are listed.
    """
    return endpoints.list_endpoints(_exposure.get().allowed)


@_tool
def call_garmin_endpoint(endpoint: str, args: dict | None = None) -> object:
    """Call any read-only Garmin Connect endpoint, e.g. `get_sleep_data`.

    `args` maps parameter names to values, e.g. {"cdate": "2026-10-05"} (see
    `list_garmin_endpoints`). Covers sleep, heart rate, HRV, stress, body
    battery, SpO2, training status, activities and splits, devices, goals,
    badges and more (over a remote connection, only health and fitness
    endpoints, without location data). Oversized results are replaced by a
    notice; narrow the request if that happens.
    """
    return endpoints.call_endpoint(_Garmin(), endpoint, args, _exposure.get().allowed)


@_tool
def get_garmin_day(date: str, metrics: list[str] | None = None) -> dict:
    """Several Garmin metrics for one day (YYYY-MM-DD) in a single call.

    Garmin's own detail for the day; for compact numbers across days use
    get_garmin_summary. `metrics` picks endpoints (see below). Default is a
    set of daily summaries: sleep, HRV, resting HR, training readiness and
    status, max metrics, hydration and intensity minutes, with long lists
    replaced by "<N items omitted>". Metrics you name in `metrics` are
    returned in full, e.g. metrics=["sleep_data"] for the per-minute sleep
    lists. A metric that fails reports its own error without failing the
    others. Metrics:

    {metrics}
    """
    return endpoints.day_metrics(_Garmin(), date, metrics, _exposure.get().allowed)


@_tool
def get_garmin_reader_info() -> dict:
    """Which garmin-reader code this server is running, and whether it is out of date.

    `version` is garmin-reader's release, `loaded_git` the commit it was built from
    and `garminconnect` the Garmin library's version. `loaded_code` hashes
    the source (the same for the same code on any OS). `stale: true` means
    the code on disk changed after this server started; fully quit and
    reopen the client to load it.
    """
    on_disk = _code_hash()
    return {
        "version": __version__,
        "garminconnect": _package_version("garminconnect"),
        "loaded_code": _LOADED_HASH,
        "loaded_git": _LOADED_COMMIT,
        "on_disk_code": on_disk,
        "stale": on_disk != _LOADED_HASH,
        "started": _STARTED,
        "pid": os.getpid(),
    }


def build_server(
    http: HttpSettings | None = None, oauth: GarminReaderOAuthProvider | None = None
) -> FastMCP:
    """A FastMCP server with every tool. With `http`, it's set up to be
    served over Streamable HTTP on 127.0.0.1, reached through `http.public_url`,
    and its tools only reach REMOTE_ALLOWED endpoints, with location data
    stripped from every result. With `oauth` too, /mcp needs a bearer token
    from its authorization server (docs/http-server-design.md, section 2).
    """
    kwargs = {}
    if http is not None:
        kwargs = dict(
            host=http.host,
            port=http.port,
            # No MCP sessions: each request stands alone, so server restarts
            # don't strand clients. Replies are plain JSON, not SSE streams,
            # since tools send no progress or log messages.
            stateless_http=True,
            json_response=True,
            # FastMCP only allows localhost Host/Origin headers on 127.0.0.1,
            # which would reject requests arriving through the tunnel.
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[
                    http.public_host, "127.0.0.1:*", "localhost:*", "[::1]:*",
                ],
                allowed_origins=[
                    http.public_url,
                    "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                ],
            ),
        )
    if oauth is not None:
        if http is None or not http.allowed_emails:
            raise ValueError("OAuth needs HTTP settings with the public URL and allowed emails")
        oauth.configure_allowed(http.allowed_emails)
        _remove_unused_sessions(oauth.garmin_profiles())
        kwargs.update(auth_server_provider=oauth, auth=auth_settings(http.public_url))
    server = FastMCP("garmin-reader", **kwargs)
    if http is not None:
        server.custom_route("/healthz", methods=["GET"])(_healthz)
    exposure = LOCAL if http is None else REMOTE
    if oauth is not None:
        page = LoginPage(
            oauth, user_token_store, secure_cookie=http.public_url.startswith("https://"),
        )
        server.custom_route("/login", methods=["GET", "POST"])(page.handle)
        exposure = replace(
            REMOTE, login_url=f"{http.public_url}/login", garmin_session=oauth.garmin_session
        )
    for fn in _TOOLS:
        tool = _wrap(fn, exposure)
        if customize := getattr(fn, "customize", None):
            customize(tool, exposure)
        server.tool(structured_output=False)(tool)
    return server


async def _healthz(request) -> JSONResponse:
    """For the host's health checks: the server process is up. Needs no
    sign-in and says nothing else. It deliberately doesn't check Garmin: an
    outage there would make the host restart a healthy server over and over.
    """
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})


def _describe_day_metrics(tool, exposure: _Exposure) -> None:
    """Give get_garmin_day's `metrics` an enum of the endpoints this server
    accepts (only allowlisted ones over HTTP), each described in one line.
    The list depends on the server, so it's filled in here rather than in the
    docstring itself.
    """
    names = endpoints.single_day_metrics(exposure.allowed)
    descriptions = endpoints.describe(names)
    tool.__doc__ = inspect.cleandoc(get_garmin_day.__doc__).format(
        metrics="\n".join(f"- {n}: {descriptions[n]}" for n in names)
    )
    sig = inspect.signature(get_garmin_day, eval_str=True)
    allowed_type = list[Literal[tuple(names)]] | None if names else None  # none: only null
    metrics = sig.parameters["metrics"].replace(annotation=allowed_type)
    tool.__signature__ = sig.replace(
        parameters=[metrics if p.name == "metrics" else p for p in sig.parameters.values()]
    )


get_garmin_day.customize = _describe_day_metrics
mcp = build_server()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="garmin-reader-mcp",
        description="Garmin MCP server. Runs over stdio unless --http is given.",
    )
    parser.add_argument(
        "--http", action="store_true",
        help="Serve Streamable HTTP at <public URL>/mcp instead of stdio",
    )
    parser.add_argument(
        "--public-url", metavar="URL",
        help="Public base URL clients use, e.g. https://abc.trycloudflare.com "
        "(default: $GARMIN_READER_PUBLIC_URL)",
    )
    parser.add_argument(
        "--port", metavar="N",
        help="Port to listen on (default: $GARMIN_READER_PORT, else $PORT, else 8000)",
    )
    parser.add_argument(
        "--host", metavar="IP",
        help="Address to listen on (default: $GARMIN_READER_HOST or 127.0.0.1; "
        "0.0.0.0 in a container)",
    )
    parser.add_argument(
        "--allowed-emails", metavar="EMAILS",
        help="Garmin login emails allowed to sign in, comma-separated "
        "(default: $GARMIN_READER_ALLOWED_EMAILS)",
    )
    parser.add_argument(
        "--import-garmin-session", nargs=2, metavar=("EMAIL", "FOLDER"),
        help="Store the Garmin session in FOLDER (garmin_tokens.json, e.g. from "
        "`garmin-reader fetch`) as EMAIL's, for when Garmin refuses sign-ins from the "
        "server's address; then exit. EMAIL must be allowed",
    )
    parser.add_argument(
        "--revoke-all", action="store_true",
        help="Sign every connected client out (they log in again), then exit. "
        "Works while the server is running",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.revoke_all:
        return _revoke_all()
    if args.import_garmin_session:
        return _import_garmin_session(*args.import_garmin_session, args.allowed_emails)
    if not args.http:
        mcp.run()
        return 0
    try:
        http = HttpSettings.from_env(
            public_url=args.public_url, port=args.port, allowed_emails=args.allowed_emails,
            host=args.host,
        )
        if not http.allowed_emails:
            raise ValueError(
                "HTTP mode needs the Garmin emails allowed to sign in: set "
                "GARMIN_READER_ALLOWED_EMAILS or pass --allowed-emails"
            )
    except ValueError as e:
        print(f"garmin-reader-mcp: {e}", file=sys.stderr)
        return 2
    oauth = GarminReaderOAuthProvider(state_dir() / "mcp_auth.sqlite", http.public_url)
    print(
        f"garmin-reader-mcp: serving {http.public_url}/mcp from {http.host}:{http.port}; "
        f"{len(http.allowed_emails)} user(s) may sign in at {http.public_url}/login",
        file=sys.stderr,
    )
    serve_http(build_server(http, oauth))
    return 0


def _import_garmin_session(email: str, folder: str, allowed_emails: str | None) -> int:
    """For when Garmin refuses sign-ins from the server's address: a session
    signed in elsewhere becomes one allowed user's own, as if they had
    signed in on /login. A running server reloads it at the user's next
    call: the import gives that profile's stored session a new revision.
    """
    try:
        allowed = parse_allowed_emails(
            allowed_emails if allowed_emails is not None else os.environ.get("GARMIN_READER_ALLOWED_EMAILS")
        )
    except ValueError as e:
        print(f"garmin-reader-mcp: {e}", file=sys.stderr)
        return 2
    user = normalize_email(email)
    if user not in allowed:
        print(f"garmin-reader-mcp: {user} isn't in GARMIN_READER_ALLOWED_EMAILS.", file=sys.stderr)
        return 2
    try:
        api = login(token_store=Path(folder), prompt_mfa=_no_mfa, env_credentials=False)
        profile = garmin_profile_id(api)
        # As for a sign-in: never replace the user's session with one that
        # can't be renewed.
        GarminGateway().save(api, user_token_store(profile))
    except Exception as err:
        print(f"garmin-reader-mcp: couldn't use the Garmin session in {folder}: {err}", file=sys.stderr)
        return 1
    provider = GarminReaderOAuthProvider(state_dir() / "mcp_auth.sqlite", public_url="")
    try:
        # The same allowlist as the server's, so this changes nothing else.
        provider.configure_allowed(allowed)
        if (previous := provider.set_garmin_profile(user, profile)) is not None:
            shutil.rmtree(user_token_store(previous).parent, ignore_errors=True)
    finally:
        provider.close()
    print(
        f"garmin-reader-mcp: stored the Garmin session as {user}'s (Garmin profile {profile}). "
        "Don't use the original copy elsewhere any more: it would sign this one out.",
        file=sys.stderr,
    )
    return 0


def _revoke_all() -> int:
    db = state_dir() / "mcp_auth.sqlite"
    if not db.exists():
        print(f"garmin-reader-mcp: no OAuth database at {db}; nothing to revoke.", file=sys.stderr)
        return 0
    provider = GarminReaderOAuthProvider(db, public_url="")
    try:
        ended = provider.revoke_all()
    finally:
        provider.close()
    print(f"garmin-reader-mcp: signed out {ended} client authorization(s).", file=sys.stderr)
    return 0


def serve_http(server: FastMCP) -> None:
    """Like `server.run(transport="streamable-http")`, but logs why requests
    were rejected (see garmin_reader.http_log).
    """
    uvicorn.run(
        LogClientErrors(server.streamable_http_app()),
        host=server.settings.host,
        port=server.settings.port,
        log_level=server.settings.log_level.lower(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
