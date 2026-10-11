"""The /login page: the owner proves who they are by logging in to Garmin
(docs/http-server-design.md, sections 2 and 3).

Two ways in, one form:

- From /authorize (`/login?req=<id>`): after the Garmin login it completes
  the OAuth flow and sends the browser back to the client.
- Opened directly (the link in the "Garmin session expired" tool error): it
  only refreshes the stored Garmin tokens.

Either way, only the configured owner's Garmin account gets through. The
password lives only in the request handler's memory; it is never logged,
stored or echoed back. Failed attempts reach Garmin, which can lock the
account, so they are capped for the whole server.
"""

from __future__ import annotations

import hashlib
import html
import secrets
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from kcal.auth import garmin_profile_id
from kcal.oauth import KcalOAuthProvider

# Failed attempts (wrong password or MFA code, wrong account, Garmin
# refusing) allowed per window, for everyone together: through the tunnel
# every request comes from 127.0.0.1, so per-IP limits mean nothing.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 60 * 60
# How long the MFA step may take, and how many can be in progress.
MFA_SECONDS = 5 * 60
MAX_MFA_FLOWS = 20

CSRF_COOKIE = "kcal_csrf"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    # No scripts at all. (No form-action: after sign-in the browser is
    # redirected to the client, which form-action would block.)
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
}


class GarminGateway:
    """The Garmin calls the page makes; tests swap in a fake."""

    def start(self, email: str, password: str) -> tuple[Any, bool]:
        """Log in with credentials. Returns (session, needs_mfa)."""
        api = Garmin(email=email, password=password, return_on_mfa=True)
        # Straight to the credential login: Garmin.login() would first load a
        # token store (GARMINTOKENS) if one is configured, and then never
        # check the email and password at all.
        status, _ = api.client.login(email, password, return_on_mfa=True)
        return api, status == "needs_mfa"

    def finish(self, api: Any, code: str) -> None:
        api.resume_login(None, code)

    def mfa_pending(self, api: Any) -> bool:
        """Whether Garmin still holds the verification step open. garminconnect
        0.3.17 keeps it after a wrong (or rate-limited) code, so the owner can
        retype the code. If the flag ever disappears, this says no and the
        owner starts over, as before.
        """
        return bool(getattr(api.client, "_mfa_pending", False))

    def profile_id(self, api: Any) -> int:
        return garmin_profile_id(api)

    def save(self, api: Any, token_store: Path) -> None:
        # Only a session with refresh tokens can be loaded again later; don't
        # overwrite stored tokens that may still work with one that can't.
        if not (api.client.di_token and api.client.di_refresh_token):
            raise IncompleteSession("Garmin signed in without issuing reusable tokens")
        api.client.dump(str(token_store))


class IncompleteSession(Exception):
    pass


@dataclass
class _MfaFlow:
    api: Any
    request_id: str | None
    expires_at: float
    # One code at a time reaches Garmin for this step.
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    # The response that finished the step, for any later submission of it.
    completed: tuple[int, bytes, list] | None = None


@dataclass
class _Submission:
    """The outcome of one form submission, replayed if the same form is
    submitted again (a double click, Enter plus click, a browser retry).
    Otherwise the browser shows the second response and loses the first,
    which may be the redirect back to the client.
    """

    done: anyio.Event
    expires_at: float
    response: tuple[int, bytes, list] | None = None


class LoginPage:
    def __init__(
        self,
        oauth: KcalOAuthProvider,
        owner_id: int,
        token_store: Path,
        on_login: Callable[[], None],
        garmin: GarminGateway | None = None,
        secure_cookie: bool = True,
    ):
        self.oauth = oauth
        self.owner_id = owner_id
        self.token_store = token_store
        self.on_login = on_login  # new Garmin tokens saved: drop any cached session
        self.garmin = garmin or GarminGateway()
        # False only for an http://localhost public URL, where browsers may
        # drop Secure cookies.
        self.secure_cookie = secure_cookie
        self._failures: deque[float] = deque()
        # Garmin sign-ins still running count toward the cap, so parallel
        # requests can't all pass the check before any failure is recorded.
        self._in_flight = 0
        self._mfa: dict[str, _MfaFlow] = {}
        self._submissions: dict[str, _Submission] = {}

    # --- entry point -----------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        if request.method == "GET":
            return self._form(request, request.query_params.get("req") or None)
        form = await request.form()
        request_id = str(form.get("req", "")) or None
        token = request.cookies.get(CSRF_COOKIE)
        if not token or not secrets.compare_digest(token, str(form.get("csrf", ""))):
            return self._dead_end(request, "Form expired", "This form expired.", request_id)
        action = form.get("action")
        if action == "deny":
            return self._deny(request, request_id)
        if action == "return":
            back = self.oauth.return_to_client(request_id) if request_id else None
            if back is None:
                return self._dead_end(
                    request, "Request expired", "This connection request is no longer known.",
                    request_id,
                )
            return self._secure(RedirectResponse(back, status_code=303))
        if action == "mfa":
            flow = str(form.get("flow", ""))
            code = str(form.get("code", "")).strip()
            finished = self._mfa.get(flow)
            if finished is not None and finished.completed is not None:
                # The step already succeeded: whatever was submitted, send the
                # browser where the success went (even for a code whose
                # earlier failure is remembered).
                return self._replay(finished.completed)
            # Keyed by the code too: a repeated submission of the same code is
            # replayed, but a corrected code is really tried.
            return await self._once(
                f"mfa:{flow}:{hashlib.sha256(code.encode()).hexdigest()}",
                lambda: self._mfa_step(request, flow, request_id, str(form.get("code", ""))),
            )
        return await self._once(
            f"signin:{form.get('attempt', '')}" if form.get("attempt") else None,
            lambda: self._signin(
                request, request_id, str(form.get("email", "")), str(form.get("password", ""))
            ),
        )

    async def _once(self, key: str | None, run) -> Response:
        """Run a submission once per key; repeats get the first one's response."""
        if key is None:
            return await run()
        now = time.time()
        self._submissions = {k: s for k, s in self._submissions.items() if s.expires_at > now}
        if (seen := self._submissions.get(key)) is not None:
            await seen.done.wait()
            if seen.response is not None:
                return self._replay(seen.response)
        while len(self._submissions) >= MAX_MFA_FLOWS * 5:
            del self._submissions[min(self._submissions, key=lambda k: self._submissions[k].expires_at)]
        entry = self._submissions[key] = _Submission(anyio.Event(), now + MFA_SECONDS)
        try:
            response = await run()
            if response.status_code == 429 or not getattr(response, "kcal_replayable", True):
                # Paused, or Garmin failed temporarily: the same form must
                # really be tried again later, not get this answer again.
                del self._submissions[key]
            else:
                entry.response = self._snapshot(response)
            return response
        finally:
            entry.done.set()

    @staticmethod
    def _snapshot(response: Response) -> tuple[int, bytes, list]:
        return response.status_code, bytes(response.body), list(response.raw_headers)

    @staticmethod
    def _replay(snapshot: tuple[int, bytes, list]) -> Response:
        status, body, headers = snapshot
        replay = Response(body, status_code=status)
        replay.raw_headers = list(headers)
        return replay

    # --- steps -------------------------------------------------------------------

    async def _signin(self, request, request_id, email, password) -> Response:
        if request_id is not None and self.oauth.pending(request_id) is None:
            return self._expired_request(request, request_id)
        if locked := self._locked(request, request_id):
            return locked
        if not email or not password:
            return self._form(request, request_id, error="Enter your Garmin email and password.", email=email)
        with self._attempt():
            try:
                api, needs_mfa = await anyio.to_thread.run_sync(self.garmin.start, email, password)
            except Exception as err:
                return self._failed(request, request_id, err, email)
            if needs_mfa:
                now = time.time()
                self._mfa = {k: f for k, f in self._mfa.items() if f.expires_at > now}
                while len(self._mfa) >= MAX_MFA_FLOWS:
                    del self._mfa[min(self._mfa, key=lambda k: self._mfa[k].expires_at)]
                flow = secrets.token_urlsafe(32)
                self._mfa[flow] = _MfaFlow(api, request_id, now + MFA_SECONDS)
                return self._mfa_form(request, flow, request_id)
            return await self._finish(request, api, request_id)

    async def _mfa_step(self, request, flow_id, request_id, code) -> Response:
        flow = self._mfa.get(flow_id)
        if flow is None or flow.expires_at <= time.time():
            self._mfa.pop(flow_id, None)
            return self._dead_end(
                request, "Sign-in expired",
                "The verification step expired or was already used.", request_id,
            )
        async with flow.lock:
            if flow.completed is not None:
                # Already finished, e.g. by an earlier submission with another
                # code: send the browser where that one went, not to an error.
                return self._replay(flow.completed)
            if flow.expires_at <= time.time():
                # Expired while this submission waited for the one before it.
                self._mfa.pop(flow_id, None)
            if self._mfa.get(flow_id) is not flow:
                return self._dead_end(
                    request, "Sign-in expired",
                    "The verification step expired or was already used.", request_id,
                )
            if locked := self._locked(request, flow.request_id):
                return locked  # the code can still be entered once the pause ends
            with self._attempt():
                try:
                    await anyio.to_thread.run_sync(self.garmin.finish, flow.api, code.strip())
                except Exception as err:
                    message = self._record_failure(err, what="code")
                    if self.garmin.mfa_pending(flow.api):
                        # Garmin keeps the step open: let the owner retype the code.
                        retry = self._mfa_form(request, flow_id, flow.request_id, error=message)
                        if isinstance(err, (GarminConnectConnectionError,
                                            GarminConnectTooManyRequestsError)):
                            # Temporary: the same code may well work later.
                            retry.kcal_replayable = False
                        return retry
                    del self._mfa[flow_id]
                    return self._form(request, flow.request_id, error=message)
                response = await self._finish(request, flow.api, flow.request_id)
                flow.completed = self._snapshot(response)
                return response

    async def _finish(self, request, api, request_id) -> Response:
        try:
            profile_id = await anyio.to_thread.run_sync(self.garmin.profile_id, api)
        except Exception as err:
            return self._failed(request, request_id, err, "")
        if profile_id != self.owner_id:
            self._failures.append(time.time())
            return self._form(
                request, request_id,
                error="That Garmin account isn't the one this server belongs to.",
            )
        try:
            await anyio.to_thread.run_sync(self.garmin.save, api, self.token_store)
        except Exception as err:
            reason = (
                "Garmin didn't issue a session kcal can keep"
                if isinstance(err, IncompleteSession)
                else "kcal couldn't save the Garmin session"
            )
            return self._form(
                request, request_id, error=f"Signed in, but {reason}. Please try again."
            )
        await anyio.to_thread.run_sync(self.on_login)
        if request_id is None:
            return self._page(
                "Garmin session refreshed",
                "<p>You're signed in to Garmin again. Go back to your chat and ask "
                "again; there's no need to reconnect the connector.</p>",
            )
        try:
            back = self.oauth.complete_authorization(request_id, subject=str(profile_id))
        except KeyError:
            return self._expired_request(request, request_id)
        return self._secure(RedirectResponse(back, status_code=303))

    def _deny(self, request, request_id) -> Response:
        back = self.oauth.deny_authorization(request_id) if request_id else None
        if back is None:
            back = self.oauth.return_to_client(request_id) if request_id else None
        if back is None:
            return self._page("Cancelled", "<p>Nothing was connected. You can close this page.</p>")
        return self._secure(RedirectResponse(back, status_code=303))

    # --- failures ------------------------------------------------------------------

    @contextmanager
    def _attempt(self):
        """Reserve a slot until credential AND owner checks have finished."""
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1

    def _locked(self, request, request_id) -> Response | None:
        now = time.time()
        while self._failures and self._failures[0] <= now - FAILURE_WINDOW_SECONDS:
            self._failures.popleft()
        if len(self._failures) + self._in_flight < MAX_FAILURES:
            return None
        oldest = self._failures[0] if self._failures else now
        minutes = int((oldest + FAILURE_WINDOW_SECONDS - now) // 60) + 1
        return self._dead_end(
            request, "Too many attempts",
            f"There were too many failed sign-ins. To protect your Garmin account from "
            f"being locked, sign-in is paused for about {minutes} minute(s).",
            request_id, status=429,
        )

    def _failed(self, request, request_id, err: Exception, email: str) -> Response:
        return self._form(request, request_id, error=self._record_failure(err), email=email)

    def _record_failure(self, err: Exception, what: str = "") -> str:
        """Count the failure toward the limit (unless Garmin was simply
        unreachable) and say what went wrong.
        """
        if isinstance(err, GarminConnectConnectionError):
            # Garmin unreachable: not the owner's fault, not counted.
            return "Couldn't reach Garmin. Please try again in a moment."
        self._failures.append(time.time())
        if isinstance(err, GarminConnectTooManyRequestsError):
            return "Garmin is limiting sign-ins right now. Please wait a few minutes."
        if what == "code":
            return "Garmin didn't accept that code. Check it and try again."
        return "Garmin didn't accept that. Check your email, password or code and try again."

    def _expired_request(self, request, request_id) -> Response:
        return self._dead_end(
            request, "Request expired",
            "This connection request has expired or was already used.", request_id,
        )

    def _dead_end(self, request, title: str, message: str, request_id, status: int = 400) -> Response:
        """An error page that always offers a way on: start again while the
        connection request is valid, go back to the client once it has
        expired, or start a plain Garmin sign-in.
        """
        csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        body = f"<p>{html.escape(message, quote=False)}</p>"
        pending = self.oauth.pending(request_id) if request_id else None
        expired = self.oauth.expired(request_id) if request_id else None
        if pending is not None:
            body += (
                f'<a class="button" href="/login?req={html.escape(request_id)}">Start again</a>'
                f'<form method="post">{self._hidden(csrf, req=request_id)}'
                f'<button class="secondary" name="action" value="deny">Cancel and go back to '
                f'{html.escape(self._host(pending))}</button></form>'
            )
        elif expired is not None:
            body += (
                f'<form method="post">{self._hidden(csrf, req=request_id)}'
                f'<button name="action" value="return">Back to '
                f'{html.escape(self._host(expired))} to connect again</button></form>'
            )
        elif request_id is not None:
            body += "<p>Go back to Claude or ChatGPT and connect again.</p>"
        else:
            body += '<a class="button" href="/login">Start again</a>'
        return self._with_csrf(self._page(title, body, status), csrf)

    @staticmethod
    def _host(p) -> str:
        return urlsplit(str(p.params.redirect_uri)).netloc

    # --- rendering ---------------------------------------------------------------------

    def _form(self, request, request_id, error: str = "", email: str = "") -> Response:
        if request_id is not None:
            pending = self.oauth.pending(request_id)
            if pending is None:
                return self._expired_request(request, request_id)
            name = pending.client.client_name or "An app"
            back_to = self._host(pending)
            intro = (
                f"<p><b>{html.escape(name)}</b> wants read access to your Garmin "
                f"health data. Afterwards you'll be sent back to "
                f"<b>{html.escape(back_to)}</b>. If you didn't start this, deny it.</p>"
            )
        else:
            intro = "<p>Sign in to Garmin to refresh kcal's Garmin session.</p>"
        csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        hidden = self._hidden(csrf, req=request_id or "")
        attempt = self._hidden(csrf, attempt=secrets.token_urlsafe(16))[len(self._hidden(csrf)):]
        body = (
            f"{intro}{self._error(error)}"
            f'<form method="post">{hidden}{attempt}'
            f'<label>Garmin email<input name="email" type="email" autocomplete="username" '
            f'value="{html.escape(email)}" required></label>'
            f'<label>Password<input name="password" type="password" '
            f'autocomplete="current-password" required></label>'
            f'<button name="action" value="signin">Sign in</button></form>'
        )
        if request_id is not None:
            body += (
                f'<form method="post">{hidden}'
                f'<button class="secondary" name="action" value="deny">Deny</button></form>'
            )
        return self._with_csrf(self._page("Sign in to Garmin", body), csrf)

    def _mfa_form(self, request, flow: str, request_id, error: str = "") -> Response:
        csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        start_over = f"/login?req={html.escape(request_id)}" if request_id else "/login"
        body = (
            "<p>Garmin sent you a verification code. Enter it to finish signing in.</p>"
            f"{self._error(error)}"
            f'<form method="post">{self._hidden(csrf, flow=flow, req=request_id or "")}'
            f'<label>Code<input name="code" inputmode="numeric" autocomplete="one-time-code" '
            f'required autofocus></label>'
            f'<button name="action" value="mfa">Verify</button></form>'
            f'<a class="button secondary" href="{start_over}">Start over</a>'
        )
        return self._with_csrf(self._page("Verification code", body), csrf)

    @staticmethod
    def _hidden(csrf: str, **fields: str) -> str:
        items = {"csrf": csrf, **fields}
        return "".join(
            f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in items.items()
        )

    @staticmethod
    def _error(message: str) -> str:
        return f'<p class="error">{html.escape(message, quote=False)}</p>' if message else ""

    def _page(self, title: str, body: str, status: int = 200) -> Response:
        page = (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>kcal: {html.escape(title)}</title><style>{_CSS}</style></head>"
            f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
        )
        return self._secure(HTMLResponse(page, status_code=status))

    @staticmethod
    def _secure(response: Response) -> Response:
        response.headers.update(SECURITY_HEADERS)
        return response

    def _with_csrf(self, response: Response, csrf: str) -> Response:
        response.set_cookie(
            CSRF_COOKIE, csrf, httponly=True, secure=self.secure_cookie, samesite="strict",
            path="/login", max_age=60 * 60,
        )
        return response


_CSS = """
body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
main{max-width:26rem;margin:3rem auto;padding:1.5rem;background:#fff;border-radius:12px;
box-shadow:0 1px 4px #0002}
h1{font-size:1.3rem;margin-top:0}
label{display:block;margin:.8rem 0;font-weight:600}
input{display:block;width:100%;box-sizing:border-box;margin-top:.3rem;padding:.55rem;
font:inherit;border:1px solid #c5cad3;border-radius:8px}
button{width:100%;padding:.65rem;margin-top:.6rem;font:inherit;font-weight:600;border:0;
border-radius:8px;background:#1d4ed8;color:#fff;cursor:pointer}
button.secondary{background:#e5e7eb;color:#1d2330}
a.button{display:block;box-sizing:border-box;text-align:center;text-decoration:none;
padding:.65rem;margin-top:.6rem;font-weight:600;border-radius:8px;background:#1d4ed8;color:#fff}
a.button.secondary{background:#e5e7eb;color:#1d2330}
.error{color:#b91c1c;font-weight:600}
@media (prefers-color-scheme:dark){body{background:#111318;color:#e6e8ec}
main{background:#1b1e25;box-shadow:none}input{background:#111318;color:inherit;border-color:#3a3f4b}
button.secondary,a.button.secondary{background:#2a2f3a;color:#e6e8ec}.error{color:#f87171}}
"""
