"""Saved Volt Code chats, so a conversation can be continued later.

Each chat is one JSON file in ~/.local/state/volt-ai/volt-code/, tagged with
the project folder it ran in and the kind of brain it used. The saved
messages are the brain's own conversation; a chat continues only on the same
kind of brain, and always with today's instructions (system prompt).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def sessions_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "volt-ai" / "volt-code"


def _plain(value):
    """SDK content blocks (Claude) become plain JSON; dicts and lists pass through."""
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _first_user_text(messages: list[dict]) -> str:
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                    return block["text"].strip()
    return ""


@dataclass
class Session:
    id: str
    folder: str
    brain: str
    title: str = ""
    updated: float = 0.0
    messages: list = field(default_factory=list)

    @classmethod
    def new(cls, folder: Path, brain: str) -> "Session":
        return cls(id=time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
                   folder=str(Path(folder).resolve()), brain=brain)

    @property
    def path(self) -> Path:
        return sessions_dir() / f"{self.id}.json"

    @staticmethod
    def _conversation(brain) -> list:
        # Local and online brains keep their instructions as the first message.
        msgs = list(brain.messages)
        if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
            msgs = msgs[1:]
        return msgs

    def save(self, brain) -> None:
        conversation = _plain(self._conversation(brain))
        if not conversation:
            return
        self.messages = conversation
        self.title = self.title or _first_user_text(conversation)[:80] or "(untitled)"
        self.updated = time.time()
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self.__dict__, fh)
        tmp.replace(path)

    def restore_into(self, brain) -> bool:
        """Load this chat into a fresh brain of the same kind. False if the kind differs."""
        if getattr(brain, "label", None) != self.brain:
            return False
        head = [m for m in brain.messages[:1] if isinstance(m, dict) and m.get("role") == "system"]
        brain.messages = head + list(self.messages)
        return True

    def recap(self, limit: int = 3) -> list[tuple[str, str]]:
        """The last few (who, text) exchanges, for showing when a chat resumes."""
        lines = []
        for msg in self.messages:
            role, content = msg.get("role"), msg.get("content")
            text = content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
            if role in ("user", "assistant") and text and text.strip():
                lines.append(("You" if role == "user" else "Volt", text.strip()))
        return lines[-limit * 2:]


def _load(path: Path) -> Optional[Session]:
    try:
        return Session(**json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def list_for(folder: Path) -> list[Session]:
    """Saved chats from this folder, newest first."""
    target = str(Path(folder).resolve())
    found = [s for s in (_load(p) for p in sessions_dir().glob("*.json")) if s and s.folder == target]
    return sorted(found, key=lambda s: s.updated, reverse=True)


def latest(folder: Path) -> Optional[Session]:
    chats = list_for(folder)
    return chats[0] if chats else None
