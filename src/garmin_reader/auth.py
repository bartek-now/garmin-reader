"""Garmin Connect authentication with cached sessions.

Private client hooks and upgrade checks: docs/garmin-library-upgrades.md.
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Callable
from pathlib import Path

from garminconnect import Garmin, GarminConnectConnectionError, GarminConnectTooManyRequestsError

from garmin_reader.settings import garmin_token_store


class GarminLoginError(Exception):
    """Logging in to Garmin failed: no usable cached session, and no way to
    get one without the owner (credentials or an MFA code).
    """


class GarminRefreshUnavailable(GarminConnectConnectionError):
    """A temporary refresh failure; retry later without replacing the session."""


def is_refresh_unavailable(error: BaseException | None) -> bool:
    # Garmin's getter decorator wraps connection exceptions, retaining their
    # causes. Distinguish refresh outages from an optional endpoint's 404.
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, GarminRefreshUnavailable):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def garmin_profile_id(api: Garmin) -> int:
    """The account's stable ID: what KCAL_GARMIN_OWNER holds."""
    return int(api.client.connectapi("/userprofile-service/socialProfile")["profileId"])


def _prompt_mfa() -> str:
    return input("Enter Garmin MFA code: ").strip()


def login(
    email: str | None = None,
    password: str | None = None,
    token_store: Path | None = None,
    prompt_mfa: Callable[[], str] = _prompt_mfa,
    env_credentials: bool = True,
) -> Garmin:
    """Log in to Garmin Connect, reusing a cached session when available.

    Credentials are only needed the first time (or after the cached session
    expires) - after that, `login()` resumes from `token_store` (default
    GARMIN_READER_STATE_DIR/garmin_tokens, i.e. ~/.garmin-reader/garmin_tokens). Without
    `env_credentials`, GARMIN_EMAIL and GARMIN_PASSWORD are ignored: a
    user's session must never be replaced by the environment's account.
    """
    token_store = token_store or garmin_token_store()
    token_store.parent.mkdir(parents=True, exist_ok=True)

    if env_credentials:
        email = email or os.environ.get("GARMIN_EMAIL")
        password = password or os.environ.get("GARMIN_PASSWORD")

    api = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    _guard_token_refresh(api.client)
    credential_login = api.client.login

    def checked_login(*args, **kwargs):
        # Loading cached tokens must not fall back to a password login just
        # because the refresh service is temporarily unavailable.
        if failure := getattr(api.client, "garmin_reader_refresh_error", None):
            raise failure
        return credential_login(*args, **kwargs)

    api.client.login = checked_login
    try:
        api.login(str(token_store))
    except Exception:
        # Garmin.login also catches refresh errors while loading a cache and
        # may subsequently report missing credentials. Keep the real cause.
        if failure := getattr(api.client, "garmin_reader_refresh_error", None):
            raise failure from None
        raise
    finally:
        api.client.login = credential_login
    return api


def _guard_token_refresh(client) -> None:
    """Preserve temporary DI refresh failures swallowed by garminconnect.

    Reuse the client's reentrant token lock (0.3.17+) so the temporary HTTP
    hook and recorded error are protected even when refresh is called
    directly. No separate garmin-reader lock is needed. See the upgrade guide.
    """
    refresh = client._refresh_session
    if getattr(refresh, "garmin_reader_refresh_guarded", False):
        return
    lock = client._token_lock
    if not isinstance(lock, type(threading.RLock())):
        raise TypeError("Incompatible garminconnect client: _token_lock must be a threading.RLock")

    @functools.wraps(refresh)
    def guarded(*args, **kwargs):
        with lock:
            client.garmin_reader_refresh_error = None
            if not getattr(client, "di_token", None):
                return refresh(*args, **kwargs)
            post = client._http_post

            def checked_post(*post_args, **post_kwargs):
                try:
                    response = post(*post_args, **post_kwargs)
                except Exception as err:
                    # No HTTP response: a transport failure, not evidence
                    # that Garmin revoked the refresh token.
                    client.garmin_reader_refresh_error = GarminRefreshUnavailable(
                        "Garmin token refresh could not reach Garmin"
                    )
                    raise client.garmin_reader_refresh_error from err
                if response.status_code == 429:
                    client.garmin_reader_refresh_error = GarminConnectTooManyRequestsError(
                        "Garmin token refresh is rate limited"
                    )
                elif response.status_code == 408 or response.status_code >= 500:
                    client.garmin_reader_refresh_error = GarminRefreshUnavailable(
                        "Garmin token refresh is temporarily unavailable"
                    )
                if client.garmin_reader_refresh_error is not None:
                    raise client.garmin_reader_refresh_error
                return response

            client._http_post = checked_post
            try:
                result = refresh(*args, **kwargs)
                if client.garmin_reader_refresh_error is not None:
                    raise client.garmin_reader_refresh_error
                return result
            finally:
                client._http_post = post

    guarded.garmin_reader_refresh_guarded = True
    client._refresh_session = guarded
