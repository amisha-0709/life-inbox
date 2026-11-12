"""Startup and authentication must fail closed without touching a personal DB."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_SECRET = "startup-tests-only-private-signing-secret-2026"
LOCAL_DEFAULT_SECRET = "local-dev-only-session-secret-change-me"
POSTGRES_URL = "postgresql+psycopg://test:test@127.0.0.1:1/test"


@pytest.fixture()
def startup_environment(tmp_path):
    """Do not inherit Google credentials, paid API keys, or the user's DB URL."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LIFE_INBOX_", "GOOGLE_", "OPENAI_"))
        and key not in {"DATABASE_URL", "SESSION_SECRET"}
    }
    environment.update(
        DATABASE_URL=f"sqlite:///{tmp_path / 'startup.db'}",
        LIFE_INBOX_UPLOAD_DIR=str(tmp_path / "uploads"),
        LIFE_INBOX_ENV="local",
        LIFE_INBOX_ALLOW_DEV_LOGIN="0",
    )
    return environment


def run_python(script: str, environment: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )


def uvicorn_startup(environment: dict[str, str], log_path: Path) -> tuple[bool, int, str]:
    """Observe real Uvicorn startup; stop immediately if invalid config starts."""
    with log_path.open("w") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 10
            while process.poll() is None:
                if "Application startup complete" in log_path.read_text():
                    break
                if time.monotonic() >= deadline:
                    pytest.fail("Uvicorn neither started nor rejected configuration in 10s")
                time.sleep(0.025)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
    log = log_path.read_text()
    return "Application startup complete" in log, returncode, log


@pytest.mark.parametrize("environment_name", ["beta", "production"])
@pytest.mark.parametrize("secret", [None, "too-short", LOCAL_DEFAULT_SECRET])
def test_uvicorn_rejects_unsafe_nonlocal_signing_secret(
    startup_environment, tmp_path, environment_name, secret
):
    startup_environment.update(
        LIFE_INBOX_ENV=environment_name,
        DATABASE_URL=POSTGRES_URL,
    )
    if secret is not None:
        startup_environment["LIFE_INBOX_SESSION_SECRET"] = secret

    started, returncode, log = uvicorn_startup(
        startup_environment, tmp_path / "uvicorn.log"
    )
    assert not started, log
    assert returncode != 0, log
    assert "LIFE_INBOX_SESSION_SECRET" in log


def test_uvicorn_rejects_sqlite_for_beta(startup_environment, tmp_path):
    startup_environment.update(
        LIFE_INBOX_ENV="beta", LIFE_INBOX_SESSION_SECRET=TEST_SECRET
    )
    started, returncode, log = uvicorn_startup(
        startup_environment, tmp_path / "uvicorn.log"
    )
    assert not started, log
    assert returncode != 0, log
    assert "PostgreSQL" in log


def test_uvicorn_rejects_unknown_environment(startup_environment, tmp_path):
    startup_environment.update(
        LIFE_INBOX_ENV="prodution",
        LIFE_INBOX_SESSION_SECRET=TEST_SECRET,
        DATABASE_URL=POSTGRES_URL,
    )
    started, returncode, log = uvicorn_startup(
        startup_environment, tmp_path / "uvicorn.log"
    )
    assert not started, log
    assert returncode != 0, log
    assert "LIFE_INBOX_ENV" in log


def test_uvicorn_rejects_incomplete_google_config_for_beta(
    startup_environment, tmp_path
):
    startup_environment.update(
        LIFE_INBOX_ENV="beta",
        LIFE_INBOX_SESSION_SECRET=TEST_SECRET,
        DATABASE_URL=POSTGRES_URL,
        LIFE_INBOX_AUTH_MODE="google",
    )
    started, returncode, log = uvicorn_startup(
        startup_environment, tmp_path / "uvicorn.log"
    )
    assert not started, log
    assert returncode != 0, log
    assert "GOOGLE_CLIENT_ID" in log


def test_local_default_still_supports_capture_analysis_and_recall(startup_environment):
    """The laptop quick start needs neither a secret nor Google credentials."""
    result = run_python(
        """
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

command.upgrade(Config('alembic.ini'), 'head')
from app.main import app

with TestClient(app) as client:
    health = client.get('/api/health')
    assert health.status_code == 200, health.text
    assert health.json()['status'] == 'ok'
    assert health.json()['environment'] == 'local'
    saved = client.post('/api/items', json={'raw_text': 'Need to renew car insurance tomorrow.'})
    assert saved.status_code == 200, saved.text
    analyzed = client.post('/api/analyze')
    assert analyzed.status_code == 200, analyzed.text
    assert analyzed.json()['mode'] == 'fallback_no_api_call'
    tasks = client.get('/api/tasks')
    assert tasks.status_code == 200, tasks.text
    assert any(saved.json()['id'] in task['source_item_ids'] for task in tasks.json())
    answer = client.post('/api/ask', json={'question': 'What about car insurance?'})
    assert answer.status_code == 200, answer.text
    assert saved.json()['id'] in {citation['item_id'] for citation in answer.json()['citations']}
""",
        startup_environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_nonlocal_session_signer_never_uses_local_fallback(startup_environment):
    startup_environment.update(LIFE_INBOX_ENV="beta", DATABASE_URL=POSTGRES_URL)
    result = run_python(
        """
from app.auth import create_session_token
from app.config import ConfigError

try:
    create_session_token(1)
except ConfigError:
    pass
else:
    raise AssertionError('A nonlocal session was signed without a private secret')
""",
        startup_environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("environment_name", ["beta", "production"])
def test_valid_nonlocal_configuration_loads_without_connecting_to_database(
    startup_environment, environment_name
):
    startup_environment.update(
        DATABASE_URL=POSTGRES_URL,
        LIFE_INBOX_ENV=environment_name,
        LIFE_INBOX_SESSION_SECRET=TEST_SECRET,
        LIFE_INBOX_AUTH_MODE="google",
        LIFE_INBOX_ALLOW_DEV_LOGIN="1",
        GOOGLE_CLIENT_ID="test-client-id",
        GOOGLE_CLIENT_SECRET="test-client-secret",
        LIFE_INBOX_PUBLIC_BASE_URL="https://inbox.example.test",
    )
    result = run_python(
        """
from app.config import load_config
config = load_config()
assert config.is_postgres
assert not config.allow_dev_login
assert config.google_oauth_configured
assert config.google_redirect_uri == 'https://inbox.example.test/api/auth/google/callback'

from fastapi.testclient import TestClient
from app.main import app
with TestClient(app, base_url='https://testserver') as client:
    health = client.get('/api/health')
    assert health.status_code == 200, health.text
    assert health.json()['database'] == 'postgres'
    assert client.get('/api/items').status_code == 401
    assert client.post('/api/auth/dev-login', json={'email': 'test@example.com'}).status_code == 403
    # Google's consent screen links here, so it must load without signing in.
    privacy = client.get('/static/privacy.html')
    assert privacy.status_code == 200, privacy.text
    assert 'Life Inbox Privacy Policy' in privacy.text
""",
        startup_environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_malformed_session_cookies_return_401_in_beta(startup_environment):
    """Malformed claims must never crash middleware or expose Local Owner data."""
    startup_environment["LIFE_INBOX_SESSION_SECRET"] = TEST_SECRET
    result = run_python(
        """
import hashlib
import hmac
import json
import os
import time
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

command.upgrade(Config('alembic.ini'), 'head')
from app.main import app
from app.auth import SESSION_COOKIE, create_session_token, parse_session_token

with TestClient(app, base_url='https://testserver') as client:
    saved = client.post('/api/items', json={'raw_text': 'PRIVATE_LOCAL_OWNER_SENTINEL'})
    assert saved.status_code == 200, saved.text
    # Retain the isolated SQLite engine, but exercise nonlocal request ownership
    # with a valid deployable configuration after startup.
    os.environ['LIFE_INBOX_ENV'] = 'beta'
    os.environ['DATABASE_URL'] = 'postgresql+psycopg://test:test@127.0.0.1:1/test'
    key = hashlib.sha256(os.environ['LIFE_INBOX_SESSION_SECRET'].encode()).digest()
    malformed_claims = [
        [],
        {},
        {'uid': 1, 'exp': 'tomorrow'},
        {'uid': 'not-an-id', 'exp': int(time.time()) + 3600},
        {'uid': True, 'exp': int(time.time()) + 3600},
    ]
    # The HTTP client's cookie encoder rejects Unicode; exercise that parser
    # boundary directly, then send ASCII cookie values through middleware.
    assert parse_session_token('body.non-ascii-\\u00e9') is None
    tokens = ['garbage.signature']
    for claims in malformed_claims:
        body = json.dumps(claims, separators=(',', ':'))
        signature = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
        tokens.append(f'{body}.{signature}')
    expired = {'uid': 1, 'exp': int(time.time()) - 1}
    body = json.dumps(expired, separators=(',', ':'))
    tokens.append(body + '.' + hmac.new(key, body.encode(), hashlib.sha256).hexdigest())
    tokens.append(create_session_token(987654))
    for token in tokens:
        client.cookies.set(SESSION_COOKIE, token)
        response = client.get('/api/items')
        assert response.status_code == 401, (token, response.status_code, response.text)
        assert 'PRIVATE_LOCAL_OWNER_SENTINEL' not in response.text
""",
        startup_environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("primary", "alias", "valid"),
    [
        (None, TEST_SECRET, True),
        (TEST_SECRET, "invalid-alias", True),
        ("too-short", TEST_SECRET, False),
        (LOCAL_DEFAULT_SECRET, TEST_SECRET, False),
    ],
)
def test_nonlocal_secret_alias_and_precedence(
    startup_environment, primary, alias, valid
):
    startup_environment.update(
        LIFE_INBOX_ENV="beta", DATABASE_URL=POSTGRES_URL, SESSION_SECRET=alias
    )
    if primary is not None:
        startup_environment["LIFE_INBOX_SESSION_SECRET"] = primary
    result = run_python(
        f"""
from app.auth import create_session_token
from app.config import ConfigError, load_config
try:
    config = load_config()
    create_session_token(1)
except ConfigError:
    assert not {valid!r}, 'Valid alias configuration rejected'
else:
    assert {valid!r}, 'Invalid primary secret bypassed using alias'
    assert config.session_secret == {TEST_SECRET!r}
""",
        startup_environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("environment_name", ["beta", "production", "beta ", "production "])
def test_nonlocal_cookie_security_uses_normalized_environment(
    startup_environment, environment_name
):
    startup_environment.update(
        LIFE_INBOX_ENV=environment_name,
        DATABASE_URL=POSTGRES_URL,
        LIFE_INBOX_SESSION_SECRET=TEST_SECRET,
    )
    result = run_python(
        """
from fastapi import Response
from app.auth import create_oauth_state, set_oauth_state_cookie, set_session_cookie
for setter, value in [(set_session_cookie, 1), (set_oauth_state_cookie, create_oauth_state())]:
    response = Response()
    setter(response, value)
    cookie = response.headers['set-cookie']
    assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=lax' in cookie
""",
        startup_environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
