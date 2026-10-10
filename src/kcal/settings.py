"""Settings shared by the CLI and the MCP server, read from the environment."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PORT = 8000
DEFAULT_HOST = "127.0.0.1"
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Where kcal keeps Garmin tokens (and, in HTTP mode, auth state)."""
    value = env.get("KCAL_STATE_DIR")
    return Path(value).expanduser() if value else Path.home() / ".kcal"


def garmin_token_store(env: Mapping[str, str] = os.environ) -> Path:
    return state_dir(env) / "garmin_tokens"


@dataclass(frozen=True)
class HttpSettings:
    """How the HTTP MCP server is reached. It listens on `host`:`port`
    (127.0.0.1 by default, for a tunnel on the same machine; 0.0.0.0 in a
    container); the public URL is where clients reach it.
    """

    public_url: str
    port: int = DEFAULT_PORT
    host: str = DEFAULT_HOST
    # Garmin profile ID allowed to log in on /login (`kcal whoami` prints it).
    garmin_owner: int | None = None

    @property
    def public_host(self) -> str:
        """The Host header clients send, e.g. `abc.trycloudflare.com`."""
        return urlsplit(self.public_url).netloc

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] = os.environ,
        public_url: str | None = None,
        port: int | str | None = None,
        garmin_owner: int | str | None = None,
        host: str | None = None,
    ) -> HttpSettings:
        """Explicit arguments (from CLI flags) win over KCAL_PUBLIC_URL,
        KCAL_PORT (or PORT, which hosting platforms such as Cloud Run set),
        KCAL_HOST and KCAL_GARMIN_OWNER. Raises ValueError naming the setting
        that's wrong.
        """
        url = public_url or env.get("KCAL_PUBLIC_URL")
        if not url:
            raise ValueError(
                "HTTP mode needs the server's public URL: set KCAL_PUBLIC_URL "
                "or pass --public-url, e.g. https://abc.trycloudflare.com"
            )
        return cls(
            public_url=_check_url(url),
            port=_check_port(
                port if port is not None else env.get("KCAL_PORT") or env.get("PORT")
            ),
            host=_check_host(host or env.get("KCAL_HOST") or DEFAULT_HOST),
            garmin_owner=_check_owner(
                garmin_owner if garmin_owner is not None else env.get("KCAL_GARMIN_OWNER")
            ),
        )


def _check_url(url: str) -> str:
    parts = urlsplit(url.strip())
    # First, so no message below echoes credentials into the log.
    if "@" in parts.netloc:
        raise ValueError("KCAL_PUBLIC_URL must not contain a user name or password")
    local = parts.hostname in _LOCAL_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ValueError(
            f"KCAL_PUBLIC_URL must be https:// (http:// only for localhost): {url!r}"
        )
    try:
        parts.port  # raises for a non-numeric or out-of-range port
    except ValueError:
        raise ValueError(f"KCAL_PUBLIC_URL has an invalid port: {url!r}") from None
    if not parts.hostname or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(
            f"KCAL_PUBLIC_URL must be just scheme and host, e.g. "
            f"https://abc.trycloudflare.com: {url!r}"
        )
    # Hosts are case-insensitive, but Host/Origin and the OAuth resource are
    # compared as strings: use the lowercase form everywhere.
    netloc = parts.netloc.lower()
    # Clients leave a default port out of the Host and Origin headers, which
    # must match exactly, so leave it out here too.
    if parts.port == {"https": 443, "http": 80}[parts.scheme]:
        netloc = netloc.rsplit(":", 1)[0]
    return f"{parts.scheme}://{netloc}"


def _check_host(value: str) -> str:
    """An IP address to listen on, e.g. 127.0.0.1, 0.0.0.0 or ::."""
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise ValueError(
            f"KCAL_HOST must be an IP address to listen on, e.g. 127.0.0.1 or "
            f"0.0.0.0: {value!r}"
        ) from None


def _check_owner(value: int | str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        raise ValueError(
            f"KCAL_GARMIN_OWNER must be a Garmin profile ID (a number; run "
            f"`kcal whoami`): {value!r}"
        ) from None


def _check_port(value: int | str | None) -> int:
    if value is None or value == "":
        return DEFAULT_PORT
    try:
        port = int(value)
    except ValueError:
        raise ValueError(f"KCAL_PORT must be a number: {value!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"KCAL_PORT must be between 1 and 65535: {port}")
    return port
