"""MCP server exposing Garmin daily steps/calories/weight as a tool.

Runs over stdio by default, or over Streamable HTTP with `--http` (see
docs/http-server-design.md). Login is non-interactive: run `kcal fetch` once
in a terminal first so the session token (and any MFA code) is cached under
KCAL_STATE_DIR (default ~/.kcal).
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import inspect
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
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
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from kcal import endpoints, summary
from kcal.auth import GarminLoginError, login
from kcal.daily import resolve_days
from kcal.http_log import LogClientErrors
from kcal.login_page import LoginPage
from kcal.oauth import KcalOAuthProvider, auth_settings
from kcal.settings import HttpSettings, garmin_token_store, state_dir

# Filled by @_tool; build_server() registers them on each FastMCP instance.
_TOOLS = []


@dataclass(frozen=True)
class _Exposure:
    """What a server's tools may reach and how their results are filtered."""

    allowed: frozenset[str] | None  # endpoint names without `get_`; None = all
    scrub: Callable[[Any], Any]
    # Where the owner refreshes an expired Garmin session (HTTP with OAuth).
    login_url: str | None = None


LOCAL = _Exposure(allowed=None, scrub=lambda result: result)
REMOTE = _Exposure(allowed=endpoints.REMOTE_ALLOWED, scrub=endpoints.strip_private)
# Fails closed: code running outside a tool wrapper gets the remote rules.
# stdio tools unlock LOCAL explicitly in _wrap().
_exposure: ContextVar[_Exposure] = ContextVar("kcal_exposure", default=REMOTE)

_SRC = Path(__file__).resolve().parent


def _code_hash() -> str:
    """Short hash of the package's .py files as they are on disk right now."""
    h = hashlib.sha256()
    for f in sorted(_SRC.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "describe", "--always", "--dirty"],
            cwd=_SRC, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


_LOADED_HASH = _code_hash()
_LOADED_COMMIT = _git_commit()
_STARTED = datetime.now().astimezone().isoformat(timespec="seconds")

_api = None
_api_lock = threading.Lock()


def _get_api():
    """Log in once per server process and reuse the session. Tools run in
    worker threads, so concurrent first calls must not both log in.
    """
    global _api
    with _api_lock:
        if _api is None:
            try:
                _api = login(prompt_mfa=_no_mfa)
            except Exception as err:
                raise _login_error(err) from err
    return _api


def _reset_api() -> None:
    """Forget the cached Garmin session (new tokens were saved, or Garmin
    rejected the current ones); the next call logs in again.
    """
    global _api
    with _api_lock:
        _api = None


def _login_error(err: Exception) -> GarminLoginError:
    url = _exposure.get().login_url
    if url is None:
        return GarminLoginError(str(err))
    if isinstance(err, (GarminConnectConnectionError, GarminConnectTooManyRequestsError)):
        # Garmin unreachable or limiting requests: passes on its own, and
        # sending the owner to sign in would only spend sign-in attempts.
        return GarminLoginError(
            "Garmin is unreachable or limiting requests right now, so kcal can't "
            "read Garmin data. This usually passes on its own: try again in a few "
            "minutes. There's no need to sign in again or reconnect the connector."
        )
    return GarminLoginError(garmin_expired_message(url))


def garmin_expired_message(login_url: str) -> str:
    """The only cue the owner gets, read by a model that knows nothing about
    kcal, so it says everything (design section 3).
    """
    return (
        "Garmin session expired, so kcal can't read Garmin data right now. "
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
        if allowed is not None and name == "connectapi":
            return _range_endpoint_only
        if allowed is not None and not (
            name.startswith("get_") and name.removeprefix("get_") in allowed
        ):
            raise ValueError(f"Garmin {name!r} is not available remotely.")
        return getattr(_get_api(), name)


def _range_endpoint_only(path: str, **kwargs):
    """Remotely, raw requests are allowed only for the range endpoints
    get_garmin_summary uses, matched in full.
    """
    if not summary.RANGE_PATHS.fullmatch(path) or kwargs:
        raise ValueError(f"Garmin path {path!r} is not available remotely.")
    return _get_api().connectapi(path)


def _no_mfa() -> str:
    raise RuntimeError(
        "Garmin login needs an MFA code or fresh credentials. Run `kcal fetch` "
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

    def run(**kwargs):
        token = _exposure.set(exposure)
        try:
            result = exposure.scrub(fn(**kwargs))
        except GarminConnectAuthenticationError as err:
            # Garmin rejected the session mid-call (expired or revoked).
            _reset_api()
            raise _login_error(err) from err
        finally:
            _exposure.reset(token)
        return endpoints.to_json(endpoints.cap_size(result))

    @functools.wraps(fn)
    async def wrapper(**kwargs):
        return await anyio.to_thread.run_sync(functools.partial(run, **kwargs))

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
def get_kcal_server_info() -> dict:
    """Which kcal code this server is running, and whether it is out of date.

    `stale: true` means the code on disk changed after this server started;
    fully quit and reopen the client to load it.
    """
    on_disk = _code_hash()
    return {
        "loaded_code": _LOADED_HASH,
        "loaded_git": _LOADED_COMMIT,
        "on_disk_code": on_disk,
        "stale": on_disk != _LOADED_HASH,
        "started": _STARTED,
        "pid": os.getpid(),
    }


def build_server(
    http: HttpSettings | None = None, oauth: KcalOAuthProvider | None = None
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
            host="127.0.0.1",
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
        if http is None or http.garmin_owner is None:
            raise ValueError("OAuth needs HTTP settings with the public URL and Garmin owner")
        kwargs.update(auth_server_provider=oauth, auth=auth_settings(http.public_url))
    server = FastMCP("kcal", **kwargs)
    exposure = LOCAL if http is None else REMOTE
    if oauth is not None:
        page = LoginPage(
            oauth, http.garmin_owner, garmin_token_store(), on_login=_reset_api,
            secure_cookie=http.public_url.startswith("https://"),
        )
        server.custom_route("/login", methods=["GET", "POST"])(page.handle)
        exposure = replace(REMOTE, login_url=f"{http.public_url}/login")
    for fn in _TOOLS:
        tool = _wrap(fn, exposure)
        if customize := getattr(fn, "customize", None):
            customize(tool, exposure)
        server.tool(structured_output=False)(tool)
    return server


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
        prog="kcal-mcp",
        description="Garmin MCP server. Runs over stdio unless --http is given.",
    )
    parser.add_argument(
        "--http", action="store_true",
        help="Serve Streamable HTTP at <public URL>/mcp instead of stdio",
    )
    parser.add_argument(
        "--public-url", metavar="URL",
        help="Public base URL clients use, e.g. https://abc.trycloudflare.com "
        "(default: $KCAL_PUBLIC_URL)",
    )
    parser.add_argument(
        "--port", metavar="N",
        help="Local port to listen on, on 127.0.0.1 (default: $KCAL_PORT or 8000)",
    )
    parser.add_argument(
        "--garmin-owner", metavar="ID",
        help="Garmin profile ID allowed to sign in (default: $KCAL_GARMIN_OWNER; "
        "`kcal whoami` prints yours)",
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
    if not args.http:
        mcp.run()
        return 0
    try:
        http = HttpSettings.from_env(
            public_url=args.public_url, port=args.port, garmin_owner=args.garmin_owner
        )
        if http.garmin_owner is None:
            raise ValueError(
                "HTTP mode needs the Garmin account allowed to sign in: set "
                "KCAL_GARMIN_OWNER or pass --garmin-owner (run `kcal whoami` to "
                "get your profile ID)"
            )
    except ValueError as e:
        print(f"kcal-mcp: {e}", file=sys.stderr)
        return 2
    oauth = KcalOAuthProvider(state_dir() / "mcp_auth.sqlite", http.public_url)
    print(
        f"kcal-mcp: serving {http.public_url}/mcp from 127.0.0.1:{http.port}; "
        f"clients sign in at {http.public_url}/login",
        file=sys.stderr,
    )
    serve_http(build_server(http, oauth))
    return 0


def _revoke_all() -> int:
    db = state_dir() / "mcp_auth.sqlite"
    if not db.exists():
        print(f"kcal-mcp: no OAuth database at {db}; nothing to revoke.", file=sys.stderr)
        return 0
    provider = KcalOAuthProvider(db, public_url="")
    try:
        ended = provider.revoke_all()
    finally:
        provider.close()
    print(f"kcal-mcp: signed out {ended} client authorization(s).", file=sys.stderr)
    return 0


def serve_http(server: FastMCP) -> None:
    """Like `server.run(transport="streamable-http")`, but logs why requests
    were rejected (see kcal.http_log).
    """
    uvicorn.run(
        LogClientErrors(server.streamable_http_app()),
        host=server.settings.host,
        port=server.settings.port,
        log_level=server.settings.log_level.lower(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
