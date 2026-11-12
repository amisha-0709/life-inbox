"""On-device Whisper transcription (model faked; never downloaded in tests)."""

from types import SimpleNamespace

from app.capture import voice_extract


def fake_local_model(monkeypatch, segments=None, *, error=None):
    calls = []

    class FakeModel:
        def transcribe(self, audio, **kwargs):
            calls.append((audio.read(), kwargs))
            if error is not None:
                raise error
            return iter(segments or []), SimpleNamespace(language="en")

    monkeypatch.setenv("LIFE_INBOX_LOCAL_TRANSCRIPTION", "1")
    monkeypatch.setattr(voice_extract, "_local_whisper_model", lambda: FakeModel())
    return calls


def test_local_whisper_used_without_openai_key(monkeypatch):
    calls = fake_local_model(monkeypatch, [SimpleNamespace(text=" Submit the project "),
                                           SimpleNamespace(text="by tomorrow. ")])
    result = voice_extract.transcribe_audio(b"audio-bytes", original_filename="n.webm",
                                            mime_type="audio/webm")
    assert result == {"text": "Submit the project by tomorrow.", "provider": "local_whisper",
                      "available": True, "note": None}
    assert calls[0][0] == b"audio-bytes"


def test_local_whisper_failure_keeps_note_with_empty_transcript(monkeypatch):
    fake_local_model(monkeypatch, error=RuntimeError("model download failed"))
    result = voice_extract.transcribe_audio(b"x", original_filename="n.webm", mime_type="audio/webm")
    assert result["provider"] == "unavailable"
    assert result["text"] == ""
    assert "manually" in result["note"]


def test_silence_reports_no_spoken_text(monkeypatch):
    fake_local_model(monkeypatch, [])
    result = voice_extract.transcribe_audio(b"x", original_filename="n.webm", mime_type="audio/webm")
    assert result["available"] is True
    assert result["note"] == "Transcription returned no spoken text."


def test_openai_key_still_takes_priority(monkeypatch):
    import openai

    fake_local_model(monkeypatch, [SimpleNamespace(text="local")])
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-live-key")
    monkeypatch.setattr(openai, "OpenAI", lambda: SimpleNamespace(audio=SimpleNamespace(
        transcriptions=SimpleNamespace(create=lambda **_k: SimpleNamespace(text="from openai")))))
    result = voice_extract.transcribe_audio(b"x", original_filename="n.webm", mime_type="audio/webm")
    assert result["provider"] == "openai"
    assert result["text"] == "from openai"


def test_local_transcription_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("LIFE_INBOX_LOCAL_TRANSCRIPTION", "0")
    result = voice_extract.transcribe_audio(b"x", original_filename="n.webm", mime_type="audio/webm")
    assert result["provider"] == "none"
