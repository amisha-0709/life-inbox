"""Optional Claude (Anthropic) provider for organizing and Ask My Inbox.

Claude is used when ANTHROPIC_API_KEY is set; otherwise the existing OpenAI
path (OPENAI_API_KEY) or the built-in no-key behavior runs. Callers keep their
own prompts and JSON validation; this module only sends one prompt and returns
the reply text. Any failure raises, so callers fall back exactly as they do
for OpenAI errors.
"""

import os
from typing import Optional

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
# One output cap for every call: organizing a large inbox returns long JSON.
MAX_OUTPUT_TOKENS = 32000


class ClaudeResponseError(RuntimeError):
    """Claude returned no usable text (refusal, truncation, or empty reply)."""


def claude_enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def claude_model() -> str:
    return os.getenv("CLAUDE_MODEL", "").strip() or DEFAULT_CLAUDE_MODEL


def claude_effort() -> str:
    return os.getenv("CLAUDE_EFFORT", "").strip() or "medium"


def complete(prompt: str) -> tuple[str, Optional[int]]:
    """Send one user prompt to Claude. Returns (reply text, input tokens)."""
    import anthropic

    client = anthropic.Anthropic()
    # Streaming keeps long organizer replies clear of HTTP timeouts.
    # Server-side fallbacks retry a safety-classifier decline on another model.
    with client.beta.messages.stream(
        model=claude_model(),
        max_tokens=MAX_OUTPUT_TOKENS,
        output_config={"effort": claude_effort()},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise ClaudeResponseError("Claude declined the request")
    if message.stop_reason == "max_tokens":
        raise ClaudeResponseError("Claude reply was cut off at the output limit")

    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    if not text.strip():
        raise ClaudeResponseError("Claude returned no text")

    usage = getattr(message, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    return text, input_tokens if isinstance(input_tokens, int) else None
