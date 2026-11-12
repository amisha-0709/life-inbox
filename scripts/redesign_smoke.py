"""Disposable Chrome walkthrough for the staged UI redesign.

Run with the project Python, for example:
  .venv/bin/python scripts/redesign_smoke.py --stage shell

Stages can run independently; --stage all combines the full interaction and
owner-isolation checks. Use --full for 160 captures and at least 150 saved tasks.
Use --full with any implemented stage for the 150+ task fixture.

Requires Node 22+ and Chrome/Chromium. Uses no personal database/profile and no
provider key. URL retrieval and voice transcription are explicit local fixtures;
PDF/image validation and extraction otherwise use the application code.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import requests


ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def chrome_binary(explicit):
    choices = [explicit, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser")]
    for choice in choices:
        if choice and Path(choice).is_file():
            return str(choice)
    raise RuntimeError("Chrome/Chromium was not found. Supply --chrome /path/to/browser.")


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def write_fixture_app(artifacts):
    # A historical UI is mounted beside the current UI against the same disposable
    # backend so screenshots compare the interface, not personal or different data.
    baseline = artifacts / "before-static"
    baseline.mkdir()
    for filename in ("styles.css", "app.js", "pagination_state.js"):
        content = subprocess.check_output([
            "git", "show", f"f743db5:app/static/{filename}",
        ], cwd=ROOT)
        (baseline / filename).write_bytes(content)
    html = subprocess.check_output([
        "git", "show", "f743db5:app/templates/index.html",
    ], cwd=ROOT, text=True).replace("/static/", "/before-static/")
    (artifacts / "before.html").write_text(html)
    (artifacts / "clock.txt").write_text("2026-10-30T10:00:00+00:00")
    (artifacts / "fixture_app.py").write_text(f'''
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import app.main as main
import app.database as database
import app.attention.dates as dates
app = main.app
class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime.fromisoformat(Path({str(artifacts / 'clock.txt')!r}).read_text())
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
database.datetime = Clock
dates.datetime = Clock
def fixture_url(url):
    if url not in {{'https://smoke.example/article', 'https://smoke.example/fail',
                   'https://smoke.example/fail-desktop', 'https://smoke.example/fail-mobile',
                   'https://smoke.example/fail-tasks-desktop', 'https://smoke.example/fail-tasks-mobile'}}:
        raise ValueError('This smoke run permits only its local URL fixtures.')
    return url
def fixture_fetch(url):
    fixture_url(url)
    if '/fail' in url and url not in _failed_once:
        _failed_once.add(url)
        raise ValueError('Fixture: page could not be extracted. Original URL is saved.')
    return {{'title':'Workshop reading', 'raw_text':'The design workshop meets in the Cedar room. Bring the blue notebook.', 'mime_type':'text/html'}}
main.validate_public_http_url = fixture_url
main.fetch_url = fixture_fetch
_failed_once = set()
main.process_voice_bytes = lambda data, **kwargs: {{
    'title':'Workshop voice note', 'raw_text':'Call the workshop technician tomorrow.',
    'extracted_text':'Call the workshop technician tomorrow.', 'mime_type':kwargs.get('mime_type','audio/webm')}}
app.mount('/before-static', StaticFiles(directory={str(baseline)!r}), name='before-static')
@app.get('/before', response_class=HTMLResponse)
def before():
    return Path({str(artifacts / 'before.html')!r}).read_text()
''')


def seed(base_url, full):
    count = 160 if full else 8
    session = requests.Session()
    headers = {"X-Life-Inbox-Timezone": "Asia/Kolkata"}
    for index in range(count):
        timing = ["today", "tomorrow", ""][index % 3]
        response = session.post(f"{base_url}/api/items", json={
            "raw_text": f"Call colleague{index + 1} {timing}".strip(),
        }, headers=headers, timeout=10)
        response.raise_for_status()
    # Genuine long reference text, a dated obligation, uncertainty and a duplicate.
    examples = [
        "Workshop reference: " + "Readable long source material about thoughtful design. " * 26,
        "Pay the workshop invoice on 2026-10-28",
        "Book the room tomorrow at 10am",
        "Call the dentist",
        "Call the dentist",
        "Workshop location: the Cedar room is on the second floor.",
    ]
    for text in examples:
        response = session.post(f"{base_url}/api/items", json={"raw_text": text}, headers=headers, timeout=10)
        response.raise_for_status()
    failed = session.post(f"{base_url}/api/items/url", json={"url": "https://smoke.example/fail"}, headers=headers, timeout=10)
    failed.raise_for_status()
    assert failed.json()["processing_status"] == "failed"
    for _ in range((count + len(examples) + 7) // 8):
        response = session.post(f"{base_url}/api/analyze", timeout=90)
        response.raise_for_status()
    tasks = []
    seen = set()
    for offset in range(0, 10000, 100):
        page = session.get(f"{base_url}/api/tasks?status=all&limit=100&offset={offset}", timeout=20).json()
        new = [task for task in page if task["id"] not in seen]
        tasks.extend(new)
        seen.update(task["id"] for task in new)
        if len(page) != 100 or not new:
            break
    if full:
        assert len(tasks) >= 150, len(tasks)
    return {"seeded_items": count + len(examples) + 1, "seeded_tasks": len(tasks),
            "external_services": "URL fetch and voice transcription use local fixtures; no provider calls"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("shell", "inbox", "tasks", "ask", "all"), default="shell")
    parser.add_argument("--full", action="store_true", help="Seed 160 captures / at least 150 tasks")
    parser.add_argument("--viewport", choices=("both", "desktop", "mobile"), default="both")
    parser.add_argument("--chrome", help="Chrome/Chromium executable")
    args = parser.parse_args()
    if not shutil.which("node"):
        raise RuntimeError("Node 22+ is required for its built-in WebSocket/CDP client.")
    browser = chrome_binary(args.chrome)
    artifacts = Path(tempfile.mkdtemp(prefix="life-inbox-redesign-", dir="/private/tmp" if Path("/private/tmp").is_dir() else None))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("LIFE_INBOX_", "GOOGLE_", "OPENAI_"))
           and key not in {"DATABASE_URL", "SESSION_SECRET"}}
    env.update(DATABASE_URL=f"sqlite:///{artifacts / 'smoke.db'}",
               LIFE_INBOX_UPLOAD_DIR=str(artifacts / "uploads"), LIFE_INBOX_ENV="local",
               LIFE_INBOX_ALLOW_DEV_LOGIN="1" if args.stage == "all" else "0", LIFE_INBOX_DISABLE_OCR="1")
    migration = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                               cwd=ROOT, env=env, capture_output=True, text=True)
    assert migration.returncode == 0, migration.stdout + migration.stderr
    write_fixture_app(artifacts)
    from tests.pdf_fixtures import make_text_pdf
    from tests.image_fixtures import make_png_bytes
    from tests.voice_fixtures import make_wav_bytes
    (artifacts / "workshop.pdf").write_bytes(make_text_pdf("Workshop reference. The Cedar room is upstairs."))
    (artifacts / "workshop.png").write_bytes(make_png_bytes(text="Workshop reference"))
    (artifacts / "workshop.wav").write_bytes(make_wav_bytes())
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = chrome = None
    print(f"Artifacts: {artifacts}", flush=True)
    with (artifacts / "uvicorn.log").open("w") as server_log, (artifacts / "chrome.log").open("w") as browser_log:
        try:
            server = subprocess.Popen([sys.executable, "-m", "uvicorn", "fixture_app:app", "--app-dir", str(artifacts),
                                       "--host", "127.0.0.1", "--port", str(port)],
                                      cwd=ROOT, env=env, stdout=server_log, stderr=server_log)
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError((artifacts / "uvicorn.log").read_text())
                try:
                    if requests.get(f"{base_url}/api/health", timeout=.3).ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError("Disposable Uvicorn did not start.")
            fixtures = seed(base_url, args.full)
            print(json.dumps(fixtures), flush=True)
            profile = artifacts / "chrome-profile"
            chrome = subprocess.Popen([browser, "--headless", "--disable-gpu", "--no-first-run", "--mute-audio",
                                       "--disable-background-networking", "--disable-sync", "--remote-debugging-port=0",
                                       "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                       f"--user-data-dir={profile}", "about:blank"],
                                      stdout=browser_log, stderr=browser_log)
            active_port = profile / "DevToolsActivePort"
            for _ in range(300):
                if active_port.exists():
                    break
                if chrome.poll() is not None:
                    raise RuntimeError((artifacts / "chrome.log").read_text())
                time.sleep(.1)
            else:
                raise RuntimeError("Chrome did not expose its temporary debugging port. " + (artifacts / "chrome.log").read_text())
            debug_port = active_port.read_text().splitlines()[0]
            result = subprocess.run(["node", str(ROOT / "scripts/redesign_smoke.mjs"), debug_port,
                                     str(port), str(artifacts), args.stage, args.viewport], cwd=ROOT, env=env,
                                    timeout=360 if args.stage == "all" else 240 if args.full else 150)
            assert result.returncode == 0, f"Browser flow failed; inspect {artifacts}"
            print(f"PASS {args.stage}; screenshots and logs: {artifacts}", flush=True)
        finally:
            stop(chrome)
            stop(server)


if __name__ == "__main__":
    # Direct execution puts scripts/, rather than the repository, on sys.path.
    sys.path.insert(0, str(ROOT))
    main()
