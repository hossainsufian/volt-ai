"""Online brain for Volt AI: Gemini, or any service that speaks the
OpenAI-compatible chat API, with the user's own API key.

Fast answers from a cloud service when the computer's own model is too slow.
It is opt-in: the user pastes a key in Settings, and messages then go to that
service. The key is stored in ~/.config/volt-ai/online_brain.json (0600) and
is never backed up or sent anywhere else.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from desktop_app.volt_claude import Stopped, build_messages

PROVIDER = "Gemini"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
KEY_PAGE = "https://aistudio.google.com/apikey"
FALLBACK_MODEL = "gemini-2.5-flash"
SKIP_WORDS = ("lite", "image", "tts", "embed", "live", "audio", "vision", "exp", "learnlm", "aqa")

SYSTEM_PROMPT = (
    "You are Volt AI, a warm, direct and genuinely helpful personal assistant. "
    "Answer clearly and get to the point; use short paragraphs or lists. When asked "
    "for code, give complete working code in fenced code blocks. Be honest when you "
    "are unsure. Today is {today}."
)


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "volt-ai" / "online_brain.json"


def load() -> Optional[dict]:
    try:
        data = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return None
    return data if data.get("key") else None


def save(key: str, model: str, base_url: str = BASE_URL) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"key": key.strip(), "model": model, "base_url": base_url}, fh)
    tmp.replace(path)


def clear() -> None:
    try:
        config_path().unlink()
    except FileNotFoundError:
        pass


def masked_key() -> Optional[str]:
    cfg = load()
    return (cfg["key"][:6] + "…" + cfg["key"][-4:]) if cfg else None


def model_name() -> Optional[str]:
    cfg = load()
    return cfg.get("model") if cfg else None


def _version(model_id: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", model_id)) or (0,)


def pick_model(ids: list[str]) -> str:
    """The newest everyday "flash" chat model from a model list."""
    names = [i.removeprefix("models/") for i in ids]
    flash = [n for n in names if "flash" in n and not any(w in n for w in SKIP_WORDS)]
    stable = [n for n in flash if "preview" not in n] or flash
    return max(stable, key=_version) if stable else (names[0] if names else FALLBACK_MODEL)


def check_key(key: str, get=None) -> str:
    """Check a key by listing its models; returns the model Volt will use."""
    if get is None:
        import requests

        get = requests.get
    resp = get(f"{BASE_URL}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    resp.raise_for_status()
    ids = [m.get("id", "") for m in (resp.json().get("data") or [])]
    return pick_model(ids)


def reply(
    turns: list[dict],
    on_text: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    extra_system: str = "",
    post=None,
) -> str:
    """Stream one answer from the online brain. Raises on network/API errors."""
    cfg = load()
    if not cfg:
        raise RuntimeError("no key")
    if post is None:
        import requests

        post = requests.post
    messages = [{"role": "system",
                 "content": SYSTEM_PROMPT.format(today=time.strftime("%A %d %B %Y")) + extra_system}]
    messages += build_messages(turns)
    body = {"model": cfg.get("model") or FALLBACK_MODEL, "messages": messages, "stream": True}
    headers = {"Authorization": f"Bearer {cfg['key']}"}
    written = ""
    with post(f"{cfg.get('base_url') or BASE_URL}/chat/completions", json=body, headers=headers,
              stream=True, timeout=(10, 300)) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines():
            line = raw.decode() if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            for choice in chunk.get("choices") or []:
                written += (choice.get("delta") or {}).get("content") or ""
            if on_text is not None and written:
                on_text(written)
            if should_stop is not None and should_stop():
                raise Stopped(written)
    return written.strip() or "No reply this time. Please try again."


def friendly_error(exc: Exception) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (400, 401, 403):
        return f"{PROVIDER} did not accept your API key. Check it in Settings."
    if status == 429:
        return f"You reached {PROVIDER}'s free limit for now. Wait a minute, or switch to the Fast brain."
    if status == 404:
        return f"{PROVIDER} doesn't offer the model Volt picked any more. Save your key again in Settings."
    name = type(exc).__name__
    if "Connection" in name or "Timeout" in name:
        return f"Can't reach {PROVIDER}. Check the internet connection, or switch to the Fast brain."
    return f"{PROVIDER} hit a problem. Try again, or switch to the Fast brain."
