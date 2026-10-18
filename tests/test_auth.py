from pathlib import Path
import threading

import pytest
from garminconnect import GarminConnectAuthenticationError

from garmin_reader import auth


class Client:
    def __init__(self):
        self._token_lock = threading.RLock()

    def _refresh_session(self):
        pass

    def login(self, *args, **kwargs):
        pytest.fail("unexpected credential fallback")


@pytest.fixture
def constructed(monkeypatch):
    seen = []

    class Garmin:
        def __init__(self, **kwargs):
            self.client = Client()
            seen.append(kwargs)

        def login(self, token_store):
            assert self.client._refresh_session.garmin_reader_refresh_guarded
            assert Path(token_store).parent.is_dir()
            seen.append(token_store)

    monkeypatch.setattr(auth, "Garmin", Garmin)
    return seen


def test_login_uses_environment_credentials_and_state_directory(constructed, monkeypatch, tmp_path):
    monkeypatch.setenv("GARMIN_EMAIL", "env@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "env-password")
    monkeypatch.setenv("GARMIN_READER_STATE_DIR", str(tmp_path / "state"))
    prompt = lambda: "123456"
    api = auth.login(prompt_mfa=prompt)
    assert constructed == [
        {"email": "env@example.com", "password": "env-password", "prompt_mfa": prompt},
        str(tmp_path / "state" / "garmin_tokens"),
    ]
    assert isinstance(api.client, Client)
    assert api.client._refresh_session.garmin_reader_refresh_guarded
    assert api.client.login.__func__ is Client.login  # temporary guard restored


def test_explicit_login_settings_win(constructed, monkeypatch, tmp_path):
    monkeypatch.setenv("GARMIN_EMAIL", "env@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "env-password")
    monkeypatch.setenv("GARMIN_READER_STATE_DIR", str(tmp_path / "ignored"))
    store = tmp_path / "explicit" / "tokens"
    api = auth.login("explicit@example.com", "explicit-password", token_store=store)
    assert constructed == [
        {"email": "explicit@example.com", "password": "explicit-password", "prompt_mfa": auth._prompt_mfa},
        str(store),
    ]
    assert api.client._refresh_session.garmin_reader_refresh_guarded
    assert not (tmp_path / "ignored").exists()


def test_cached_login_needs_no_credentials(constructed, monkeypatch, tmp_path):
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
    api = auth.login(token_store=tmp_path / "tokens")
    assert constructed[0]["email"] is None
    assert constructed[0]["password"] is None
    assert constructed[1] == str(tmp_path / "tokens")
    assert api.client._refresh_session.garmin_reader_refresh_guarded


def test_login_preserves_authentication_failure(monkeypatch, tmp_path):
    error = GarminConnectAuthenticationError("bad credentials")

    class Garmin:
        client = Client()

        def login(self, token_store):
            raise error

    api = Garmin()
    monkeypatch.setattr(auth, "Garmin", lambda **kwargs: api)
    with pytest.raises(GarminConnectAuthenticationError) as raised:
        auth.login(token_store=tmp_path / "tokens")
    assert raised.value is error
    assert api.client.login.__func__ is Client.login


def test_refresh_guard_rejects_missing_lock_before_installing():
    client = Client()
    del client._token_lock
    original = client._refresh_session
    with pytest.raises(AttributeError, match="_token_lock"):
        auth._guard_token_refresh(client)
    assert client._refresh_session == original
    assert "_refresh_session" not in vars(client)


def test_refresh_guard_rejects_non_reentrant_lock_before_installing():
    client = Client()
    client._token_lock = threading.Lock()
    original = client._refresh_session
    with pytest.raises(TypeError, match="_token_lock must be a threading.RLock"):
        auth._guard_token_refresh(client)
    assert client._refresh_session == original
    assert "_refresh_session" not in vars(client)
