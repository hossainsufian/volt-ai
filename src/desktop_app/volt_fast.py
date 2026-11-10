"""Fast mode for Volt AI: one direct, streamed call to the local Ollama model.

The full assistant pipeline (tool router, planner, memory search, digests)
makes several model calls per message, which takes 30-90 seconds on a small
CPU-only computer. Fast mode skips all of that: it sends a short system prompt
plus the recent chat to Ollama and streams the answer back word by word.
Nothing leaves the computer. It has no web search or long-term memory; the
Smart mode (the full pipeline) still offers those.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

from desktop_app.volt_claude import Stopped, build_messages

DEFAULT_MODEL = "qwen2.5:3b"
DEFAULT_URL = "http://127.0.0.1:11434"
MAX_TURNS = 12          # recent turns sent with each message; keeps prompts short
CONTEXT_TOKENS = 4096   # smaller context = faster prompt processing on CPU
KEEP_LOADED = "30m"     # keep the model in memory between messages

SYSTEM_PROMPT = (
    "You are Volt AI, a warm, direct and genuinely helpful personal assistant. "
    "Answer clearly and get to the point; use short paragraphs or lists. "
    "When asked for code, give complete working code in fenced code blocks. "
    "Be honest when you don't know something, and say that you can't browse the "
    "web or see live data in this mode. Today is {today}."
)


def _jarvis_config() -> dict:
    override = os.environ.get("JARVIS_CONFIG_PATH")
    if override:
        path = Path(override).expanduser()
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        path = (Path(base) if base else Path.home() / ".config") / "jarvis" / "config.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def model_and_url() -> tuple[str, str]:
    cfg = _jarvis_config()
    model = cfg.get("ollama_chat_model") or DEFAULT_MODEL
    url = (cfg.get("llm_base_url") or cfg.get("ollama_base_url") or DEFAULT_URL).rstrip("/")
    return model, url


def reply(
    turns: list[dict],
    on_text: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    post: Optional[Callable] = None,
) -> str:
    """Stream one answer from the local model. Raises on connection errors."""
    model, url = model_and_url()
    if post is None:
        import requests

        post = requests.post
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(today=time.strftime("%A %d %B %Y"))}]
    messages += build_messages(turns)[-MAX_TURNS:]
    if messages[1:] and messages[1]["role"] != "user":
        messages.pop(1)  # after trimming, the chat must still start with the user
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": KEEP_LOADED,
        "options": {"num_ctx": CONTEXT_TOKENS},
    }
    written = ""
    with post(f"{url}/api/chat", json=body, stream=True, timeout=(10, 300)) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            written += (chunk.get("message") or {}).get("content", "")
            if on_text is not None and written:
                on_text(written)
            if should_stop is not None and should_stop():
                raise Stopped(written)
            if chunk.get("done"):
                break
    return written.strip() or "No reply this time. Please try again."


def friendly_error(exc: Exception) -> str:
    name = type(exc).__name__
    if "Connection" in name or "Timeout" in name:
        return "The brain on this computer isn't running yet. Wait a moment and try again."
    msg = str(exc)
    if "not found" in msg.lower():
        model, _ = model_and_url()
        return f"The model {model} isn't installed on this computer."
    return "The fast brain hit a problem. Try again, or switch to Smart mode."


def warm_up(post: Optional[Callable] = None) -> None:
    """Load the model into memory ahead of the first message (best effort)."""
    model, url = model_and_url()
    try:
        if post is None:
            import requests

            post = requests.post
        post(f"{url}/api/generate", json={"model": model, "keep_alive": KEEP_LOADED}, timeout=60).close()
    except Exception:
        pass
