"""Google OAuth sign-in/sign-up with mocked token exchange."""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient


def _purge():
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name, None)


@pytest.fixture()
def oauth_client(tmp_path, monkeypatch):
    test_db = tmp_path / "oauth.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{test_db}")
    monkeypatch.setenv("LIFE_INBOX_ENV", "local")
    monkeypatch.setenv("LIFE_INBOX_ALLOW_DEV_LOGIN", "0")
    monkeypatch.setenv(
        "LIFE_INBOX_SESSION_SECRET",
        "test-session-secret-at-least-32-chars!!",
    )
    monkeypatch.setenv("LIFE_INBOX_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LIFE_INBOX_AUTH_MODE", "google")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "GOOGLE_REDIRECT_URI",
        "http://testserver/api/auth/google/callback",
    )
    monkeypatch.setenv("LIFE_INBOX_PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LIFE_INBOX_AUTO_CREATE_TABLES", "1")

    _purge()
    main = importlib.import_module("app.main")
    database = importlib.import_module("app.database")
    with TestClient(main.app) as client:
        yield client
    database.engine.dispose()
    _purge()


def test_google_start_redirects_when_configured(oauth_client):
    response = oauth_client.get("/api/auth/google/start", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert "accounts.google.com" in location
    assert "client_id=test-client-id" in location
    assert "life_inbox_oauth_state" in response.cookies


def test_google_start_unavailable_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'nogoogle.db'}")
    monkeypatch.setenv("LIFE_INBOX_ENV", "local")
    monkeypatch.setenv(
        "LIFE_INBOX_SESSION_SECRET",
        "test-session-secret-at-least-32-chars!!",
    )
    monkeypatch.setenv("LIFE_INBOX_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_REDIRECT_URI", raising=False)
    monkeypatch.delenv("LIFE_INBOX_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("LIFE_INBOX_AUTO_CREATE_TABLES", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    _purge()
    main = importlib.import_module("app.main")
    database = importlib.import_module("app.database")
    with TestClient(main.app) as client:
        response = client.get("/api/auth/google/start", follow_redirects=False)
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"].lower()
    database.engine.dispose()
    _purge()


def test_google_callback_creates_user_and_sets_session(oauth_client, monkeypatch):
    import app.auth as auth

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code
            self.text = str(payload)

        def json(self):
            return self._payload

    def fake_post(url, data=None, timeout=None):
        assert "oauth2.googleapis.com/token" in url
        assert data["code"] == "auth-code-1"
        return FakeResponse({"access_token": "access-token-1", "token_type": "Bearer"})

    def fake_get(url, headers=None, timeout=None):
        assert "userinfo" in url
        assert headers["Authorization"] == "Bearer access-token-1"
        return FakeResponse(
            {
                "sub": "google-sub-alice",
                "email": "alice@gmail.com",
                "email_verified": True,
                "name": "Alice Example",
            }
        )

    monkeypatch.setattr(auth.requests, "post", fake_post)
    monkeypatch.setattr(auth.requests, "get", fake_get)

    start = oauth_client.get("/api/auth/google/start", follow_redirects=False)
    state = start.cookies.get("life_inbox_oauth_state")
    assert state

    callback = oauth_client.get(
        "/api/auth/google/callback",
        params={"code": "auth-code-1", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    assert "life_inbox_session" in callback.cookies

    me = oauth_client.get("/api/auth/me")
    assert me.status_code == 200
    payload = me.json()
    assert payload["email"] == "alice@gmail.com"
    assert payload["display_name"] == "Alice Example"
    assert payload["auth_provider"] == "google"
    assert payload["google_sub"] == "google-sub-alice"

    # Second login restores the same owner.
    start2 = oauth_client.get("/api/auth/google/start", follow_redirects=False)
    state2 = start2.cookies.get("life_inbox_oauth_state")

    def fake_get_again(url, headers=None, timeout=None):
        return FakeResponse(
            {
                "sub": "google-sub-alice",
                "email": "alice@gmail.com",
                "email_verified": True,
                "name": "Alice Example",
            }
        )

    monkeypatch.setattr(auth.requests, "get", fake_get_again)
    callback2 = oauth_client.get(
        "/api/auth/google/callback",
        params={"code": "auth-code-1", "state": state2},
        follow_redirects=False,
    )
    assert callback2.status_code == 302
    me2 = oauth_client.get("/api/auth/me").json()
    assert me2["id"] == payload["id"]


def test_google_callback_rejects_bad_state(oauth_client, monkeypatch):
    import app.auth as auth

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"access_token": "x"}

    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: FakeResponse())
    response = oauth_client.get(
        "/api/auth/google/callback",
        params={"code": "x", "state": "not-a-valid-state"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_dev_login_disabled_when_allow_flag_off(oauth_client):
    response = oauth_client.post(
        "/api/auth/dev-login",
        json={"email": "someone@example.com"},
    )
    assert response.status_code == 403
