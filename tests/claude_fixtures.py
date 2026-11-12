"""Fake Anthropic client for tests. Never calls the live Claude API."""

from types import SimpleNamespace


def mock_claude(monkeypatch, reply_text="", *, stop_reason="end_turn", input_tokens=55,
                raise_on_create=None):
    """Set a fake ANTHROPIC_API_KEY and patch anthropic.Anthropic.

    Returns the list of keyword arguments each stream() call received.
    """
    import anthropic

    calls = []

    class FakeStream:
        def __init__(self, kwargs):
            calls.append(kwargs)

        def __enter__(self):
            if raise_on_create is not None:
                raise raise_on_create
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                stop_reason=stop_reason,
                content=[
                    SimpleNamespace(type="thinking", thinking=""),
                    SimpleNamespace(type="text", text=reply_text),
                ],
                usage=SimpleNamespace(input_tokens=input_tokens),
            )

    fake_client = SimpleNamespace(
        beta=SimpleNamespace(
            messages=SimpleNamespace(stream=lambda **kwargs: FakeStream(kwargs))
        )
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-live-key")
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: fake_client)
    return calls
