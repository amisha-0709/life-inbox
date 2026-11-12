"""Keep developer credentials and deployment settings out of automated tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_app_environment(monkeypatch):
    # Individual fixtures supply temporary databases and explicit test settings.
    # Tests that mock a live provider opt in with their own fake credentials.
    for name in tuple(os.environ):
        if name.startswith(("LIFE_INBOX_", "GOOGLE_", "OPENAI_")) or name in {
            "DATABASE_URL",
            "SESSION_SECRET",
        }:
            monkeypatch.delenv(name)
