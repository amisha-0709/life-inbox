"""On-device Whisper transcription (model faked; never downloaded in tests)."""

from types import SimpleNamespace

from app.capture import voice_extract


def fake_local_model(monkeypatch, segments=None, *, error=None, detected=("en", 0.9),
                     english_segments=None):
    calls = []

    class FakeModel:
        def transcribe(self, audio, **kwargs):
            calls.append((audio.read(), kwargs))
            if error is not None:
                raise error
            if kwargs.get("language") == "en" and english_segments is not None:
                return iter(english_segments), SimpleNamespace(language="en", language_probability=1.0)
            language = kwargs.get("language") or detected[0]
            probability = 1.0 if kwargs.get("language") else detected[1]
            return iter(segments or []), SimpleNamespace(language=language,
                                                         language_probability=probability)

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


def test_uncertain_non_english_detection_retries_as_english(monkeypatch):
    calls = fake_local_model(monkeypatch, [SimpleNamespace(text="garbled")], detected=("hi", 0.42),
                             english_segments=[SimpleNamespace(text="Submit the project by tomorrow.")])
    result = voice_extract.transcribe_audio(b"x", original_filename="n.webm", mime_type="audio/webm")
    assert result["text"] == "Submit the project by tomorrow."
    assert [c[1]["language"] for c in calls] == [None, "en"]


def test_confident_non_english_is_kept(monkeypatch):
    calls = fake_local_model(monkeypatch, [SimpleNamespace(text="kal project jama karna hai")],
                             detected=("hi", 0.93), english_segments=[SimpleNamespace(text="wrong")])
    result = voice_extract.transcribe_audio(b"x", original_filename="n.webm", mime_type="audio/webm")
    assert result["text"] == "kal project jama karna hai"
    assert len(calls) == 1


def test_forced_language_setting_skips_detection(monkeypatch):
    calls = fake_local_model(monkeypatch, [SimpleNamespace(text="namaste")], detected=("en", 0.3))
    monkeypatch.setenv("LIFE_INBOX_WHISPER_LANGUAGE", "hi")
    voice_extract.transcribe_audio(b"x", original_filename="n.webm", mime_type="audio/webm")
    assert [c[1]["language"] for c in calls] == ["hi"]
