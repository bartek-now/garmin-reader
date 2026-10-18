"""Log why the HTTP server rejected a request.

Uvicorn's access log only shows the status line, and the MCP SDK doesn't log
most of its 4xx reasons (unsupported protocol version, malformed JSON-RPC,
...). This logs the error text it sent back, plus the few request headers
that explain it. Never logged: request or response payloads beyond that
error text, and credentials (Authorization, cookies).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("garmin_reader.http")

# Request headers worth seeing next to a rejection. Deliberately an allowlist,
# so Authorization and cookies can't end up in the log.
LOGGED_HEADERS = ("mcp-protocol-version", "content-type", "accept", "user-agent")
# Error responses are short; this only guards against a large body.
MAX_ERROR_CHARS = 500


class LogClientErrors:
    """ASGI middleware: log 4xx responses (except 404) with their reason."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = None
        body = bytearray()

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and _logged(status):
                body.extend(message.get("body", b"")[: MAX_ERROR_CHARS - len(body)])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            if _logged(status):
                logger.warning(
                    "%s %s -> %s: %s [%s]",
                    scope["method"], scope["path"], status, _reason(bytes(body)),
                    _headers(scope),
                )


def _logged(status: int | None) -> bool:
    # 404s are routine (clients probing for OAuth metadata, browsers asking
    # for /favicon.ico) and say nothing beyond "Not Found".
    return status is not None and 400 <= status < 500 and status != 404


def _reason(body: bytes) -> str:
    """The JSON-RPC error message if there is one, else the body as text."""
    text = body.decode("utf-8", "replace")
    try:
        return str(json.loads(text)["error"]["message"])[:MAX_ERROR_CHARS]
    except (ValueError, KeyError, TypeError):
        return text.strip()[:MAX_ERROR_CHARS] or "(no body)"


def _headers(scope) -> str:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
    return ", ".join(f"{h}={headers[h]}" for h in LOGGED_HEADERS if h in headers)
