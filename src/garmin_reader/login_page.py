"""The /login page: a user proves who they are by logging in to Garmin
(docs/http-server-design.md, sections 2 and 3).

Two ways in, one form:

- From /authorize (`/login?req=<id>`): after the Garmin login it completes
  the OAuth flow for that user and sends the browser back to the client.
- Opened directly (the link in the "Garmin session expired" tool error): it
  only refreshes that user's stored Garmin tokens.

Either way, anyone with a Garmin account can sign in: Garmin accepting
the password proves the person owns that account. Each user's Garmin session is stored apart from
everyone else's. The password lives only in the request handler's memory;
it is never logged, stored or echoed back. Failed attempts reach Garmin,
which can lock the account, so they are capped per email.
"""

from __future__ import annotations

import hashlib
import html
import secrets
import shutil
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

from garmin_reader.auth import garmin_profile_id
from garmin_reader.oauth import GarminReaderOAuthProvider
from garmin_reader.settings import normalize_email

# Failed attempts (wrong password or MFA code, Garmin refusing) allowed per
# window, per email: each email is one Garmin account Garmin could lock.
# Behind a proxy or tunnel requests may all come from one address, so
# per-IP limits would mean nothing.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 60 * 60
# Emails with failures remembered at once. Every failure costs a Garmin
# round trip, so honest use stays far below this.
MAX_TRACKED_EMAILS = 1000
# How long the MFA step may take, and how many can be in progress.
MFA_SECONDS = 5 * 60
MAX_MFA_FLOWS = 20

CSRF_COOKIE = "garmin_reader_csrf"
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
        """Check the verification code. If the check failed without Garmin
        ever answering about the code (timeouts, server errors, bot-check
        pages), raise GarminVerificationUnavailable: garminconnect reports
        that as the same authentication error as a wrong code, so the
        answers are watched as they arrive instead.
        """
        client = api.client
        session = getattr(client, "_mfa_session", None)
        watched = None
        if session is not None:
            watched = _WatchedSession(session, widget=getattr(client, "_mfa_flow", None) == "widget")
            client._mfa_session = watched
        try:
            api.resume_login(None, code)
        except GarminConnectTooManyRequestsError:
            raise  # already says what happened
        except Exception as err:
            # Whatever the library made of it (an authentication error, or
            # in the widget flow the raw transport error), no answer about
            # the code means Garmin was unavailable.
            if watched is not None and watched.asked and not watched.answered:
                raise GarminVerificationUnavailable("Garmin didn't answer the code check") from err
            raise
        finally:
            if watched is not None and client._mfa_session is watched:
                client._mfa_session = session

    def mfa_pending(self, api: Any) -> bool:
        """Whether Garmin still holds the verification step open. garminconnect
        0.3.17 keeps it after a wrong (or rate-limited) code, so the user can
        retype the code. If the flag ever disappears, this says no and the
        user starts over, as before.
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


class GarminVerificationUnavailable(GarminConnectConnectionError):
    """Garmin's code check didn't answer: temporary, not a wrong code."""


class _WatchedSession:
    """Stands in for garminconnect's MFA session while a code is checked,
    noting whether any verification POST got an actual answer from Garmin.
    Everything else goes to the real session unchanged.
    """

    def __init__(self, session: Any, widget: bool):
        self._session = session
        self._widget = widget
        self.asked = 0
        self.answered = False

    def post(self, *args, **kwargs):
        self.asked += 1
        response = self._session.post(*args, **kwargs)  # a transport failure: no answer
        if _answered(response, self._widget):
            self.answered = True
        return response

    def __getattr__(self, name):
        return getattr(self._session, name)


def _answered(response: Any, widget: bool) -> bool:
    """Whether Garmin answered about the code. Doubtful cases count as an
    answer, so a wrong code is never mistaken for an outage (which isn't
    counted toward the failure limit).
    """
    status = getattr(response, "status_code", None)
    if status is None:
        return True
    if _no_answer_status(status):
        return False
    if widget:
        return True  # the widget flow answers with an HTML page
    try:
        body = response.json()  # the portal and mobile flows answer in JSON
    except Exception:
        return False  # e.g. a bot-check page instead of an answer
    # Garmin also reports rate limits and failures inside a 200's JSON body
    # (error.status-code, e.g. "429"), which garminconnect treats as such.
    error = body.get("error") if isinstance(body, dict) else None
    code = str(error.get("status-code", "")) if isinstance(error, dict) else ""
    return not (code.isdigit() and _no_answer_status(int(code)))


def _no_answer_status(status: int) -> bool:
    """Timeouts, rate limits and server errors: Garmin didn't decide anything.
    427 is the non-standard status Garmin's sign-in pages also block with.
    """
    return status in (408, 427, 429) or status >= 500


@dataclass
class _MfaFlow:
    api: Any
    email: str
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
        oauth: GarminReaderOAuthProvider,
        token_store: Callable[[int], Path],
        garmin: GarminGateway | None = None,
        secure_cookie: bool = True,
    ):
        self.oauth = oauth
        self.token_store = token_store  # a Garmin profile's own session store
        self.garmin = garmin or GarminGateway()
        # False only for an http://localhost public URL, where browsers may
        # drop Secure cookies.
        self.secure_cookie = secure_cookie
        # Per email: failures within the window, and Garmin sign-ins still
        # running, which count toward the cap so parallel requests can't all
        # pass the check before any failure is recorded.
        self._failures: dict[str, deque[float]] = {}
        self._in_flight: dict[str, int] = {}
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
            if (finished is not None and finished.completed is not None
                    and finished.expires_at > time.time()):
                # The step already succeeded: whatever was submitted, send the
                # browser where the success went (even for a code whose
                # earlier failure is remembered). Only while the step lasts:
                # after that the redirect's code has expired too.
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
            # The first one failed outright: this one tries for itself.
        while len(self._submissions) >= MAX_MFA_FLOWS * 5:
            del self._submissions[min(self._submissions, key=lambda k: self._submissions[k].expires_at)]
        entry = self._submissions[key] = _Submission(anyio.Event(), now + MFA_SECONDS)
        try:
            response = await run()
            # Duplicates already waiting get this answer whatever it is.
            entry.response = self._snapshot(response)
            if response.status_code == 429 or not getattr(response, "garmin_reader_replayable", True):
                # Paused, or Garmin failed temporarily: later submissions of
                # the same form must really be tried, not get this answer.
                self._forget(key, entry)
            return response
        except BaseException:
            self._forget(key, entry)
            raise
        finally:
            entry.done.set()

    def _forget(self, key: str, entry: _Submission) -> None:
        """Remove `entry`, but only if it's still the one stored under `key`:
        another submission may have replaced it meanwhile.
        """
        if self._submissions.get(key) is entry:
            del self._submissions[key]

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
        user = normalize_email(email)
        if not user or not password:
            return self._form(request, request_id, error="Enter your Garmin email and password.", email=email)
        if locked := self._locked(request, request_id, user):
            return locked
        with self._attempt(user):
            try:
                api, needs_mfa = await anyio.to_thread.run_sync(self.garmin.start, user, password)
            except Exception as err:
                return self._form(
                    request, request_id, error=self._record_failure(err, user), email=email
                )
            if needs_mfa:
                now = time.time()
                self._mfa = {k: f for k, f in self._mfa.items() if f.expires_at > now}
                while len(self._mfa) >= MAX_MFA_FLOWS:
                    del self._mfa[min(self._mfa, key=lambda k: self._mfa[k].expires_at)]
                flow = secrets.token_urlsafe(32)
                self._mfa[flow] = _MfaFlow(api, user, request_id, now + MFA_SECONDS)
                return self._mfa_form(request, flow, request_id)
            return await self._finish(request, api, user, request_id)

    async def _mfa_step(self, request, flow_id, request_id, code) -> Response:
        flow = self._mfa.get(flow_id)
        if flow is None or flow.expires_at <= time.time():
            self._mfa.pop(flow_id, None)
            return self._dead_end(
                request, "Sign-in expired",
                "The verification step expired or was already used.", request_id,
            )
        async with flow.lock:
            if flow.expires_at <= time.time():
                # Expired while this submission waited for the one before it.
                self._mfa.pop(flow_id, None)
            elif flow.completed is not None:
                # Already finished, e.g. by an earlier submission with another
                # code: send the browser where that one went, not to an error.
                return self._replay(flow.completed)
            if self._mfa.get(flow_id) is not flow:
                return self._dead_end(
                    request, "Sign-in expired",
                    "The verification step expired or was already used.", request_id,
                )
            if locked := self._locked(request, flow.request_id, flow.email):
                return locked  # the code can still be entered once the pause ends
            with self._attempt(flow.email):
                try:
                    await anyio.to_thread.run_sync(self.garmin.finish, flow.api, code.strip())
                except Exception as err:
                    message = self._record_failure(err, flow.email, what="code")
                    if self.garmin.mfa_pending(flow.api):
                        # Garmin keeps the step open: let the user retype the code.
                        retry = self._mfa_form(request, flow_id, flow.request_id, error=message)
                        if isinstance(err, (GarminConnectConnectionError,
                                            GarminConnectTooManyRequestsError)):
                            # Temporary: the same code may well work later.
                            retry.garmin_reader_replayable = False
                        return retry
                    # Another submission may have removed it meanwhile (expiry).
                    self._mfa.pop(flow_id, None)
                    return self._form(request, flow.request_id, error=message)
                response = await self._finish(request, flow.api, flow.email, flow.request_id)
                flow.completed = self._snapshot(response)
                return response

    async def _finish(self, request, api, user, request_id) -> Response:
        """`user` (an email) signed in to Garmin: keep the session
        as theirs, then finish the connection request if there is one.
        """
        try:
            profile_id = await anyio.to_thread.run_sync(self.garmin.profile_id, api)
        except Exception as err:
            return self._form(request, request_id, error=self._record_failure(err, user))
        try:
            await anyio.to_thread.run_sync(self.garmin.save, api, self.token_store(profile_id))
        except Exception as err:
            reason = (
                "Garmin didn't issue a session garmin-reader can keep"
                if isinstance(err, IncompleteSession)
                else "garmin-reader couldn't save the Garmin session"
            )
            return self._form(
                request, request_id, error=f"Signed in, but {reason}. Please try again."
            )
        # A new revision: the server reloads this profile's session.
        if (previous := self.oauth.set_garmin_profile(user, profile_id)) is not None:
            # This email used another Garmin account before, now nobody's.
            shutil.rmtree(self.token_store(previous).parent, ignore_errors=True)
        if request_id is None:
            return self._page(
                "Garmin session refreshed",
                "<p>You're signed in to Garmin again. Go back to your chat and ask "
                "again; there's no need to reconnect the connector.</p>",
            )
        try:
            back = self.oauth.complete_authorization(request_id, subject=user)
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
    def _attempt(self, user: str):
        """Reserve one of `user`'s slots until the sign-in has finished."""
        self._in_flight[user] = self._in_flight.get(user, 0) + 1
        try:
            yield
        finally:
            self._in_flight[user] -= 1
            if not self._in_flight[user]:
                del self._in_flight[user]

    def _locked(self, request, request_id, user: str) -> Response | None:
        now = time.time()
        failures = self._failures.get(user, deque())
        while failures and failures[0] <= now - FAILURE_WINDOW_SECONDS:
            failures.popleft()
        if not failures:
            self._failures.pop(user, None)
        if len(failures) + self._in_flight.get(user, 0) < MAX_FAILURES:
            return None
        oldest = failures[0] if failures else now
        minutes = int((oldest + FAILURE_WINDOW_SECONDS - now) // 60) + 1
        return self._dead_end(
            request, "Too many attempts",
            f"There were too many failed sign-ins. To protect your Garmin account from "
            f"being locked, sign-in is paused for about {minutes} minute(s).",
            request_id, status=429,
        )

    def _record_failure(self, err: Exception, user: str, what: str = "") -> str:
        """Count the failure toward `user`'s limit (unless Garmin was simply
        unreachable) and say what went wrong.
        """
        if isinstance(err, GarminConnectConnectionError):
            # Garmin unreachable: not the user's fault, not counted.
            return "Couldn't reach Garmin. Please try again in a moment."
        now = time.time()
        if user not in self._failures and len(self._failures) >= MAX_TRACKED_EMAILS:
            # Anyone can try any email, so emails tried once and never again
            # would otherwise pile up: drop expired ones, then the stalest.
            cutoff = now - FAILURE_WINDOW_SECONDS
            self._failures = {e: f for e, f in self._failures.items() if f[-1] > cutoff}
            while len(self._failures) >= MAX_TRACKED_EMAILS:
                del self._failures[min(self._failures, key=lambda e: self._failures[e][-1])]
        self._failures.setdefault(user, deque()).append(now)
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
            intro = "<p>Sign in to Garmin to refresh garmin-reader's Garmin session.</p>"
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
            f"<title>garmin-reader: {html.escape(title)}</title><style>{_CSS}</style></head>"
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
