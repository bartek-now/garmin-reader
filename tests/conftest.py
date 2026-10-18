import pytest

from garmin_reader import mcp_server


@pytest.fixture(autouse=True)
def isolated_state(tmp_path_factory, monkeypatch):
    """No test may read or change the real ~/.garmin-reader (or ~/.kcal):
    the HTTP server deletes the stored Garmin sessions of users who were
    removed, under GARMIN_READER_STATE_DIR. Tests that need a particular
    folder set it themselves. Cached Garmin sessions don't carry over
    between tests either.
    """
    state = tmp_path_factory.mktemp("garmin-reader-state")
    monkeypatch.setenv("GARMIN_READER_STATE_DIR", str(state))
    for name in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "GARMINTOKENS",
                 "KCAL_GARMIN_OWNER", "GARMIN_READER_ALLOWED_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    # Their names from before the rename, still read as fallbacks.
    for name in ("STATE_DIR", "PUBLIC_URL", "PORT", "HOST", "ALLOWED_EMAILS", "GIT_COMMIT"):
        monkeypatch.delenv("KCAL_" + name, raising=False)
    monkeypatch.setattr(mcp_server, "_api", None)
    monkeypatch.setattr(mcp_server, "_profile_sessions", {})
