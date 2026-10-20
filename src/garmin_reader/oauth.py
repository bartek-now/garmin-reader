"""OAuth 2.1 authorization server for the HTTP MCP server (design section 2).

The MCP SDK provides the endpoints (metadata, /register, /authorize, /token,
/revoke) and checks PKCE, redirect URIs, client IDs and expiry. This module
is the provider behind them: it stores registered clients and tokens, and
hands the user's browser to the /login page, which calls
`complete_authorization()` once the user has proved who they are.

Several people can use one server. Each token carries its user (the Garmin
login email, lowercase) as its subject. The users table remembers each user's Garmin profile, whose
stored session serves their data.

Clients, tokens and users live in one SQLite file, so they survive restarts
and a move to another host is copying that file. Tokens are stored as
SHA-256 hashes only. Pending authorizations and authorization codes are
short-lived and kept in memory; a restart just means logging in again.

The queries are small lookups in a local file, so they run directly on the
event loop rather than in worker threads.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SCOPE = "garmin:read"
ACCESS_TOKEN_SECONDS = 60 * 60
REFRESH_TOKEN_SECONDS = 30 * 24 * 60 * 60
CODE_SECONDS = 5 * 60
PENDING_SECONDS = 10 * 60
# An expired request is remembered this much longer, only so the login page
# can still send the browser back to the client that started it.
RECENT_SECONDS = 60 * 60

# /register and /authorize are open to anyone who finds the URL, so what they
# can make the server keep is bounded: waiting authorizations are capped
# (oldest dropped), clients that never got a token are forgotten after a
# day, and registration stops at a hard limit.
MAX_PENDING = 100
UNUSED_CLIENT_SECONDS = 24 * 60 * 60
MAX_CLIENTS = 500


class GarminReaderAccessToken(AccessToken):
    family: str  # one per authorization, kept across refreshes


class GarminReaderRefreshToken(RefreshToken):
    family: str


class GarminReaderAuthorizationCode(AuthorizationCode):
    generation: int


@dataclass
class PendingAuthorization:
    """An /authorize request waiting for the user to log in."""

    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float
    generation: int


def auth_settings(public_url: str) -> AuthSettings:
    return AuthSettings(
        issuer_url=public_url,
        resource_server_url=f"{public_url}/mcp",
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
        # Refuse tokens issued for any other resource.
        validate_token_resource=True,
    )


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _same_resource(a: str, b: str) -> bool:
    """URLs equal up to scheme/host case and a trailing slash."""
    pa, pb = urlsplit(a), urlsplit(b)
    return (
        (pa.scheme.lower(), pa.netloc.lower(), pa.path.rstrip("/"))
        == (pb.scheme.lower(), pb.netloc.lower(), pb.path.rstrip("/"))
    )


def _owner_only(path: Path, mode: int) -> None:
    """Best effort: on Windows chmod only controls the read-only flag."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class GarminReaderOAuthProvider:
    """Implements the SDK's OAuthAuthorizationServerProvider protocol."""

    def __init__(self, db_path: Path, public_url: str):
        self.public_url = public_url
        self.resource = f"{public_url}/mcp"
        if not db_path.parent.exists():
            db_path.parent.mkdir(parents=True, mode=0o700)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        _owner_only(db_path, 0o600)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    info TEXT NOT NULL,
                    registered_at INTEGER NOT NULL,
                    authorized INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY,
                    -- used_refresh: a rotated refresh token, kept until it
                    -- would have expired so a replay can be recognized.
                    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh', 'used_refresh')),
                    family TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    resource TEXT,
                    subject TEXT
                );
                CREATE INDEX IF NOT EXISTS tokens_family ON tokens (family);
                -- owner: unused since tokens carry their user; kept so
                -- older databases open unchanged.
                CREATE TABLE IF NOT EXISTS auth_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    owner TEXT,
                    generation INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO auth_state (id) VALUES (1);
                -- Whose Garmin session serves each user's data. `session`
                -- changes whenever that profile's stored session is
                -- replaced, so every server process reloads it.
                CREATE TABLE IF NOT EXISTS users (
                    email TEXT PRIMARY KEY,
                    profile_id INTEGER NOT NULL,
                    signed_in_at INTEGER NOT NULL,
                    session TEXT NOT NULL
                );
                """
            )
        self._pending: dict[str, PendingAuthorization] = {}
        self._codes: dict[str, GarminReaderAuthorizationCode] = {}
        # Tokens from before multiple users carried no email (or, for a
        # single owner, their Garmin profile ID): drop them.
        with self._lock, self._db:
            self._db.execute("DELETE FROM tokens WHERE subject IS NULL OR subject NOT LIKE '%@%'")

    def _generation(self) -> int:
        with self._lock:
            (generation,) = self._db.execute(
                "SELECT generation FROM auth_state WHERE id = 1"
            ).fetchone()
        return generation

    # --- users -----------------------------------------------------------------

    def set_garmin_profile(self, email: str, profile_id: int) -> int | None:
        """`email` signed in to Garmin as `profile_id`, whose stored session
        was just replaced: a new revision for everyone using that profile.
        Returns the profile the email used before, if it's now nobody's, so
        the caller deletes its stored session.
        """
        revision = secrets.token_hex(8)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT profile_id FROM users WHERE email = ?", (email,)
            ).fetchone()
            self._db.execute(
                "INSERT OR REPLACE INTO users (email, profile_id, signed_in_at, session) "
                "VALUES (?, ?, ?, ?)",
                (email, int(profile_id), int(time.time()), revision),
            )
            self._db.execute(
                "UPDATE users SET session = ? WHERE profile_id = ?", (revision, int(profile_id))
            )
            if row is None or row[0] == int(profile_id):
                return None
            (others,) = self._db.execute(
                "SELECT COUNT(*) FROM users WHERE profile_id = ?", (row[0],)
            ).fetchone()
        return row[0] if not others else None

    def garmin_profiles(self) -> set[int]:
        """Every Garmin profile a user's data comes from."""
        with self._lock:
            return {profile for (profile,) in self._db.execute("SELECT profile_id FROM users")}

    def garmin_session(self, email: str) -> tuple[int, str] | None:
        """(Garmin profile, revision of its stored session) serving `email`,
        if they ever signed in to Garmin.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT profile_id, session FROM users WHERE email = ?", (email,)
            ).fetchone()
        return (row[0], row[1]) if row else None

    def garmin_profile(self, email: str) -> int | None:
        link = self.garmin_session(email)
        return link[0] if link else None

    # --- clients ---------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            row = self._db.execute(
                "SELECT info FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        now = int(time.time())
        with self._lock:
            self._db.execute(
                "DELETE FROM clients WHERE authorized = 0 AND registered_at <= ?",
                (now - UNUSED_CLIENT_SECONDS,),
            )
            (count,) = self._db.execute("SELECT COUNT(*) FROM clients").fetchone()
            if count >= MAX_CLIENTS:
                raise RegistrationError(
                    "invalid_client_metadata", "Too many registered clients; try again later."
                )
            self._db.execute(
                "INSERT OR REPLACE INTO clients (client_id, info, registered_at) VALUES (?, ?, ?)",
                (client_info.client_id, client_info.model_dump_json(), now),
            )

    # --- authorization -----------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the user's browser to /login."""
        generation = self._generation()
        if params.resource is not None and not _same_resource(params.resource, self.resource):
            raise AuthorizeError(
                "invalid_request", f"This server only issues tokens for {self.resource}"
            )
        now = time.time()
        self._pending = {
            k: p for k, p in self._pending.items() if p.expires_at + RECENT_SECONDS > now
        }
        while len(self._pending) >= MAX_PENDING:
            del self._pending[min(self._pending, key=lambda k: self._pending[k].expires_at)]
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = PendingAuthorization(
            client=client, params=params, expires_at=now + PENDING_SECONDS,
            generation=generation,
        )
        return f"{self.public_url}/login?req={request_id}"

    def pending(self, request_id: str) -> PendingAuthorization | None:
        """The waiting request, for the login page to show and complete."""
        p = self._pending.get(request_id)
        return p if (p is not None and p.expires_at > time.time()
                     and p.generation == self._generation()) else None

    def expired(self, request_id: str) -> PendingAuthorization | None:
        """A request that timed out recently (see return_to_client)."""
        p = self._pending.get(request_id)
        return p if (p is not None and (p.expires_at <= time.time()
                                      or p.generation != self._generation())) else None

    def return_to_client(self, request_id: str) -> str | None:
        """For an expired request: the URL that sends the browser back to the
        client with an error, so it offers to connect again instead of
        waiting forever. Single use.
        """
        if self.expired(request_id) is None:
            return None
        p = self._pending.pop(request_id)
        return construct_redirect_uri(
            str(p.params.redirect_uri),
            error="access_denied",
            error_description="The sign-in expired. Please connect again.",
            state=p.params.state,
        )

    def _take_pending(self, request_id: str) -> PendingAuthorization | None:
        """Remove and return a request that hasn't expired. An expired one
        stays, for return_to_client().
        """
        if self.pending(request_id) is None:
            return None
        return self._pending.pop(request_id)

    def complete_authorization(self, request_id: str, subject: str) -> str:
        """The user logged in as `subject` (their email): issue a code and
        return the URL to send their browser back to the client. Single use.
        """
        p = self._take_pending(request_id)
        if p is None:
            raise KeyError("Unknown or expired authorization request")
        now = time.time()
        self._codes = {k: c for k, c in self._codes.items() if c.expires_at > now}
        code = secrets.token_urlsafe(32)
        self._codes[code] = GarminReaderAuthorizationCode(
            generation=p.generation,
            code=code,
            scopes=p.params.scopes or [SCOPE],
            expires_at=now + CODE_SECONDS,
            client_id=p.client.client_id,
            code_challenge=p.params.code_challenge,
            redirect_uri=p.params.redirect_uri,
            redirect_uri_provided_explicitly=p.params.redirect_uri_provided_explicitly,
            resource=self.resource,
            subject=subject,
        )
        return construct_redirect_uri(str(p.params.redirect_uri), code=code, state=p.params.state)

    def deny_authorization(self, request_id: str) -> str | None:
        """Send the browser back to the client with access_denied, if the
        request is still waiting.
        """
        p = self._take_pending(request_id)
        if p is None:
            return None
        return construct_redirect_uri(
            str(p.params.redirect_uri), error="access_denied", state=p.params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        return code if (code is not None and code.client_id == client.client_id
                        and code.generation == self._generation()) else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use, enforced here rather than relying on the SDK's timing.
        code = self._codes.pop(authorization_code.code, None)
        if code is None or code.client_id != client.client_id or code.expires_at <= time.time():
            raise TokenError("invalid_grant", "Authorization code already used")
        return self._issue(
            client.client_id, code.scopes, code.subject, generation=code.generation
        )

    # --- tokens --------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> GarminReaderRefreshToken | None:
        row = self._load(refresh_token, ("refresh", "used_refresh"))
        if row is None or row["client_id"] != client.client_id:
            return None
        if row.pop("kind") == "used_refresh":
            # A rotated token came back: it leaked, or a legitimate client is
            # replaying it. Either way, end the whole chain (OAuth 2.1, 4.3.1).
            self._delete_family(row["family"])
            return None
        return GarminReaderRefreshToken(token=refresh_token, **row)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: GarminReaderRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the old pair stops working, the old refresh token is kept
        # as used so a replay is caught, and the new pair joins the family.
        return self._issue(
            client.client_id, scopes, refresh_token.subject, refresh_token.family,
            refresh_token=refresh_token,
        )

    async def load_access_token(self, token: str) -> GarminReaderAccessToken | None:
        row = self._load(token, ("access",))
        if row is None:
            return None
        del row["kind"]
        return GarminReaderAccessToken(token=token, **row)

    async def revoke_token(self, token: GarminReaderAccessToken | GarminReaderRefreshToken) -> None:
        self._delete_family(token.family)

    def revoke_all(self) -> int:
        """Sign every client out (clients stay registered). Returns how many
        authorizations ended.
        """
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            (families,) = self._db.execute(
                "SELECT COUNT(DISTINCT family) FROM tokens WHERE kind != 'used_refresh'"
            ).fetchone()
            self._db.execute("DELETE FROM tokens")
            self._db.execute("UPDATE auth_state SET generation = generation + 1 WHERE id = 1")
        return families

    # --- storage -------------------------------------------------------------

    def _issue(
        self, client_id: str, scopes: list[str], subject: str | None, family: str | None = None,
        *, generation: int | None = None, refresh_token: GarminReaderRefreshToken | None = None,
    ) -> OAuthToken:
        now = int(time.time())
        family = family or secrets.token_urlsafe(16)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        rows = [
            (_hash(access), "access", now + ACCESS_TOKEN_SECONDS),
            (_hash(refresh), "refresh", now + REFRESH_TOKEN_SECONDS),
        ]
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            (current_generation,) = self._db.execute(
                "SELECT generation FROM auth_state WHERE id = 1"
            ).fetchone()
            if subject is None:
                raise TokenError("invalid_grant", "Authorization belongs to no user")
            if generation is not None and generation != current_generation:
                raise TokenError("invalid_grant", "Authorization was revoked")
            if refresh_token is not None:
                row = self._db.execute(
                    "SELECT scopes FROM tokens WHERE token_hash = ? AND kind = 'refresh' "
                    "AND client_id = ? AND family = ? AND subject = ? AND expires_at > ?",
                    (_hash(refresh_token.token), client_id, family, subject, now),
                ).fetchone()
                if row is None:
                    raise TokenError("invalid_grant", "Refresh token was revoked or used")
                if not set(scopes) <= set(json.loads(row[0])):
                    raise TokenError("invalid_scope", "Cannot expand refresh token scopes")
                self._db.execute("DELETE FROM tokens WHERE family = ? AND kind = 'access'", (family,))
                self._db.execute(
                    "UPDATE tokens SET kind = 'used_refresh' WHERE token_hash = ?",
                    (_hash(refresh_token.token),),
                )
            self._db.execute("DELETE FROM tokens WHERE expires_at <= ?", (now,))
            self._db.executemany(
                "INSERT INTO tokens (token_hash, kind, family, client_id, scopes, "
                "expires_at, resource, subject) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (h, kind, family, client_id, json.dumps(scopes), exp, self.resource, subject)
                    for h, kind, exp in rows
                ],
            )
            self._db.execute("UPDATE clients SET authorized = 1 WHERE client_id = ?", (client_id,))
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_SECONDS,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    def _load(self, token: str, kinds: tuple[str, ...]) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT kind, family, client_id, scopes, expires_at, resource, subject "
                f"FROM tokens WHERE token_hash = ? AND kind IN ({','.join('?' * len(kinds))}) "
                "AND expires_at > ?",
                (_hash(token), *kinds, int(time.time())),
            ).fetchone()
        if row is None:
            return None
        kind, family, client_id, scopes, expires_at, resource, subject = row
        if subject is None:
            return None
        return dict(
            kind=kind, family=family, client_id=client_id, scopes=json.loads(scopes),
            expires_at=expires_at, resource=resource, subject=subject,
        )

    def _delete_family(self, family: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM tokens WHERE family = ?", (family,))

    def close(self) -> None:
        self._db.close()
