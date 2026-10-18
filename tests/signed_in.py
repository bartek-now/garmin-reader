"""Tool calls as a signed-in user, as the HTTP server makes them for a
request carrying that user's OAuth token.
"""

from contextlib import contextmanager

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from garmin_reader.oauth import SCOPE, GarminReaderAccessToken


@contextmanager
def as_user(email: str):
    token = GarminReaderAccessToken(
        token="test-token", client_id="test-client", scopes=[SCOPE], subject=email,
        family="test-family",
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)
