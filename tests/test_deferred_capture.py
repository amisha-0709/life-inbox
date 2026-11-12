"""Opt-in capture saves originals before owner-bound deferred extraction."""

from contextlib import ExitStack
import importlib
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from tests.image_fixtures import make_png_bytes
from tests.pdf_fixtures import make_text_pdf
from tests.voice_fixtures import make_wav_bytes


@pytest.fixture()
def deferred(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'deferred.db'}")
    monkeypatch.setenv("LIFE_INBOX_AUTO_CREATE_TABLES", "1")
    monkeypatch.setenv("LIFE_INBOX_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LIFE_INBOX_ENV", "local")
    modules = ("app.main", "app.database", "app.auth", "app.config", "app.integrations.calendar")
    for name in modules:
        sys.modules.pop(name, None)
    main = importlib.import_module("app.main")
    db = importlib.import_module("app.database")
    queued = []
    original_add_task = main.BackgroundTasks.add_task
    monkeypatch.setattr(main.BackgroundTasks, "add_task", lambda self, function, *args, **kwargs:
                        queued.append((function, args, kwargs)))
    monkeypatch.setattr(main, "validate_public_http_url", lambda url: url)
    monkeypatch.setattr(main, "fetch_url", lambda url: {
        "title": "Saved article", "raw_text": "Article reference text", "mime_type": "text/html",
    })
    monkeypatch.setattr(main, "extract_pdf_text", lambda data, **kwargs: {
        "title": "Saved PDF", "raw_text": "PDF reference text", "extracted_text": "PDF reference text",
        "mime_type": "application/pdf",
    })
    monkeypatch.setattr(main, "process_image_bytes", lambda data, **kwargs: {
        "title": "Saved image", "raw_text": "Image reference text", "extracted_text": "Image reference text",
        "ai_description": "An image", "mime_type": "image/png", "thumbnail_bytes": make_png_bytes(),
    })
    monkeypatch.setattr(main, "process_voice_bytes", lambda data, **kwargs: {
        "title": "Saved voice", "raw_text": "Voice reference text", "extracted_text": "Voice reference text",
        "mime_type": "audio/wav",
    })
    with ExitStack() as stack:
        a = stack.enter_context(TestClient(main.app))
        b = stack.enter_context(TestClient(main.app))
        user_a = a.post("/api/auth/dev-login", json={"email": "capture-a@example.com"}).json()
        user_b = b.post("/api/auth/dev-login", json={"email": "capture-b@example.com"}).json()
        yield SimpleNamespace(main=main, db=db, a=a, b=b, user_a=user_a, user_b=user_b,
                              queued=queued, original_add_task=original_add_task)
    db.set_current_owner_id(None)
    db.engine.dispose()
    for name in modules:
        sys.modules.pop(name, None)


def capture(env, kind, *, defer=True):
    path = f"/api/items/{kind}" + ("?defer_processing=true" if defer else "")
    if kind == "url":
        return env.a.post(path, json={"url": "https://example.com/article"})
    files = {
        "pdf": ("source.pdf", make_text_pdf("Source PDF reference"), "application/pdf"),
        "image": ("source.png", make_png_bytes(), "image/png"),
        "voice": ("source.wav", make_wav_bytes(), "audio/wav"),
    }
    return env.a.post(path, files={"file": files[kind]})


@pytest.mark.parametrize("kind", ["url", "pdf", "image", "voice"])
def test_deferred_original_is_saved_before_owner_bound_processing(deferred, kind):
    response = capture(deferred, kind)
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["processing_status"] == "captured"
    assert item["owner_user_id"] == deferred.user_a["id"]
    assert item["time_reference_at"]
    assert deferred.a.get(f"/api/items/{item['id']}").json()["processing_status"] == "captured"
    assert deferred.b.get(f"/api/items/{item['id']}").status_code == 404
    if kind == "url":
        assert item["source_url"] == "https://example.com/article"
    else:
        source = deferred.a.get(f"/api/items/{item['id']}/file")
        assert source.status_code == 200
        assert source.content
    assert len(deferred.queued) == 1
    function, args, kwargs = deferred.queued[0]
    assert args[0] == deferred.user_a["id"]
    deferred.db.set_current_owner_id(None)
    function(*args, **kwargs)
    assert deferred.db.get_current_owner_id() is None
    saved = deferred.a.get(f"/api/items/{item['id']}").json()
    assert saved["processing_status"] == "ready"
    assert saved["time_reference_at"] == item["time_reference_at"]
    assert saved["extracted_text"] or saved["raw_text"]
    assert deferred.b.get("/api/items").json() == []
    assert deferred.a.get("/api/tasks").json() == []
    assert deferred.a.get("/api/analyze/latest").status_code == 204


@pytest.mark.parametrize("kind", ["url", "pdf", "image", "voice"])
def test_default_capture_remains_synchronous(deferred, kind):
    response = capture(deferred, kind, defer=False)
    assert response.status_code == 200, response.text
    assert response.json()["processing_status"] == "ready"
    assert deferred.queued == []


def test_starlette_runs_deferred_work_after_serializing_captured_response(deferred, monkeypatch):
    monkeypatch.setattr(deferred.main.BackgroundTasks, "add_task", deferred.original_add_task)
    response = capture(deferred, "url")
    assert response.status_code == 200
    item = response.json()
    assert item["processing_status"] == "captured"
    # TestClient waits for background work. The serialized response still tells
    # the truth about the initial save, while polling reveals extraction's result.
    saved = deferred.a.get(f"/api/items/{item['id']}").json()
    assert saved["processing_status"] == "ready"
    assert saved["owner_user_id"] == deferred.user_a["id"]
    assert deferred.b.get(f"/api/items/{item['id']}").status_code == 404


def test_failed_deferred_extraction_retains_source_and_can_retry(deferred, monkeypatch):
    response = capture(deferred, "url")
    item = response.json()
    assert response.status_code == 200

    def fail(_url):
        raise ValueError("Could not extract readable text")

    monkeypatch.setattr(deferred.main, "fetch_url", fail)
    function, args, kwargs = deferred.queued.pop()
    deferred.db.set_current_owner_id(deferred.user_b["id"])
    function(*args, **kwargs)
    assert deferred.db.get_current_owner_id() == deferred.user_b["id"]
    deferred.db.set_current_owner_id(None)
    saved = deferred.a.get(f"/api/items/{item['id']}").json()
    assert saved["processing_status"] == "failed"
    assert saved["source_url"] == item["source_url"]
    assert saved["error_message"] == "Could not extract readable text"
    assert deferred.b.post(f"/api/items/{item['id']}/retry").status_code == 404
    monkeypatch.setattr(deferred.main, "fetch_url", lambda _url: {
        "title": "Recovered article", "raw_text": "Recovered reference text", "mime_type": "text/html",
    })
    retried = deferred.a.post(f"/api/items/{item['id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["processing_status"] == "ready"
    assert retried.json()["id"] == item["id"]


def test_background_wrapper_restores_owner_even_when_source_is_deleted(deferred):
    response = capture(deferred, "url")
    item = response.json()
    assert deferred.a.delete(f"/api/items/{item['id']}").status_code == 204
    function, args, kwargs = deferred.queued.pop()
    deferred.db.set_current_owner_id(deferred.user_b["id"])
    function(*args, **kwargs)
    assert deferred.db.get_current_owner_id() == deferred.user_b["id"]
    deferred.db.set_current_owner_id(None)
    assert deferred.b.get("/api/items").json() == []
