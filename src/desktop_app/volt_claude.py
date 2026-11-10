"""Optional Claude API brain for Volt AI.

Off by default. The user pastes their own Anthropic API key in Settings; it is
stored only on this computer (never in the Drive backup) and every message sent
while Claude is switched on goes to Anthropic and is billed to that key.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

MODELS = {
    "claude-opus-5-5": "Claude Opus 5.5 (most capable)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (faster, cheaper)",
}
DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = (
    "You are Volt AI, a personal AI assistant. You are warm, direct and genuinely "
    "helpful. You are especially good at writing code and building websites and "
    "apps: give complete, working code in fenced code blocks, say which file each "
    "block belongs in, and explain briefly how to run it. Be honest when you are "
    "unsure, and don't flatter the user."
)


def key_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "volt-ai" / "anthropic_api_key"


def load_key() -> Optional[str]:
    try:
        key = key_path().read_text().strip()
    except OSError:
        return None
    return key or None


def save_key(key: str) -> None:
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.strip())
    tmp.replace(path)


def clear_key() -> None:
    try:
        key_path().unlink()
    except FileNotFoundError:
        pass


def masked_key() -> Optional[str]:
    key = load_key()
    if not key:
        return None
    return key[:10] + "…" + key[-4:]


def build_messages(turns: list[dict]) -> list[dict]:
    """Turn stored chat turns into a Messages API conversation."""
    out: list[dict] = []
    for turn in turns:
        role, text = turn.get("role"), (turn.get("content") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        if not out and role != "user":
            continue  # the conversation must start with the user
        out.append({"role": role, "content": text})
    return out


def friendly_error(exc: Exception) -> str:
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return "Your Claude API key was rejected. Check it in Settings."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Your Claude API key isn't allowed to use this model."
    if isinstance(exc, anthropic.RateLimitError):
        return "Claude is rate-limited right now. Wait a minute and try again."
    if isinstance(exc, anthropic.BadRequestError):
        msg = str(getattr(exc, "message", exc))
        if "credit" in msg.lower() or "balance" in msg.lower():
            return "Your Claude account is out of credit. Add credit at console.anthropic.com."
        return f"Claude couldn't handle that request: {msg}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach Claude. Check your internet connection."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude had a problem ({exc.status_code}). Try again shortly."
    return "Something went wrong talking to Claude. Try again."


class Stopped(Exception):
    """The user pressed Stop while Claude was writing."""

    def __init__(self, partial: str):
        super().__init__("stopped")
        self.partial = partial


def reply(turns: list[dict], model: str, api_key: str, client=None,
          on_text=None, should_stop=None) -> str:
    """Ask Claude for the next assistant turn. Raises on API errors.

    ``on_text(text_so_far)`` is called as words arrive; ``should_stop()`` is
    polled between chunks and raises :class:`Stopped` with the partial text.
    """
    if model not in MODELS:
        model = DEFAULT_MODEL
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
    with client.beta.messages.stream(
        model=model,
        max_tokens=64000,
        system=SYSTEM_PROMPT,
        messages=build_messages(turns),
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        # If a safety filter declines, let Anthropic retry on a suitable model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        written = ""
        for chunk in stream.text_stream:
            written += chunk
            if on_text is not None:
                on_text(written)
            if should_stop is not None and should_stop():
                raise Stopped(written)
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        return "Claude declined to answer that one. Try rephrasing your request."
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    if message.stop_reason == "max_tokens":
        text += "\n\n_(The answer was cut off because it was very long. Ask me to continue.)_"
    return text or "Claude returned an empty answer. Try again."
