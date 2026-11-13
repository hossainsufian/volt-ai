"""
Volt AI chat store.

Every chat, message and generated image is kept on disk under the Volt
data directory so the history survives app restarts and can be backed up
(see ``volt_drive_backup``) to survive a full system reset.

Layout::

    <data_dir>/chats.db     SQLite: chats + messages
    <data_dir>/images/      generated images, referenced by messages

The store is thread-safe: the web server calls it from request threads
while the desktop app records replies from the Qt main thread.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from jarvis.debug import debug_log

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    image TEXT,
    status TEXT NOT NULL DEFAULT 'done',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
"""

TITLE_MAX = 60


def default_data_dir() -> Path:
    """Return the per-user Volt data directory (XDG on Linux)."""
    override = os.environ.get("VOLT_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "volt-ai"


def _title_from(text: str) -> str:
    title = " ".join(text.split())
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1].rstrip() + "…"
    return title or "New chat"


def _with_meta(row) -> dict:
    """A message row as a dict, with its JSON settings (meta) decoded."""
    out = dict(row)
    try:
        out["meta"] = json.loads(out["meta"]) if out.get("meta") else None
    except ValueError:
        out["meta"] = None
    return out


class VoltStore:
    """Persistent chats, messages and image files."""

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.images_dir = self.data_dir / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            str(self.data_dir / "chats.db"), check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(_SCHEMA)
        columns = {r[1] for r in self._db.execute("PRAGMA table_info(messages)")}
        if "meta" not in columns:  # chats saved before messages carried settings
            self._db.execute("ALTER TABLE messages ADD COLUMN meta TEXT")
        self._db.commit()
        debug_log(f"volt store opened at {self.data_dir}", "volt")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- Chats -----------------------------------------------------------

    def create_chat(self, title: str = "New chat") -> dict:
        now = time.time()
        chat = {"id": uuid.uuid4().hex, "title": title, "created": now, "updated": now}
        with self._lock:
            self._db.execute(
                "INSERT INTO chats (id, title, created, updated) VALUES (?, ?, ?, ?)",
                (chat["id"], chat["title"], now, now),
            )
            self._db.commit()
        return chat

    def list_chats(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, title, created, updated FROM chats ORDER BY updated DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_chat(self, chat_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT id, title, created, updated FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
        return dict(row) if row else None

    def rename_chat(self, chat_id: str, title: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE chats SET title = ? WHERE id = ?", (_title_from(title), chat_id)
            )
            self._db.commit()
        return cur.rowcount > 0

    def delete_chat(self, chat_id: str) -> bool:
        with self._lock:
            images = [
                r["image"]
                for r in self._db.execute(
                    "SELECT image FROM messages WHERE chat_id = ? AND image IS NOT NULL",
                    (chat_id,),
                )
            ]
            cur = self._db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            self._db.commit()
        for name in images:
            try:
                (self.images_dir / name).unlink(missing_ok=True)
            except OSError as exc:
                debug_log(f"could not delete image {name}: {exc}", "volt")
        return cur.rowcount > 0

    # --- Messages --------------------------------------------------------

    def add_message(
        self,
        chat_id: str,
        role: str,
        text: str = "",
        *,
        image: Optional[str] = None,
        status: str = "done",
        meta: Optional[dict] = None,
    ) -> dict:
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"unknown role: {role}")
        now = time.time()
        with self._lock:
            if self.get_chat(chat_id) is None:
                raise KeyError(chat_id)
            cur = self._db.execute(
                "INSERT INTO messages (chat_id, role, text, image, status, created, meta)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, role, text, image, status, now, json.dumps(meta) if meta else None),
            )
            # The first user message names a fresh chat.
            if role == "user":
                count = self._db.execute(
                    "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND role = 'user'",
                    (chat_id,),
                ).fetchone()[0]
                if count == 1:
                    self._db.execute(
                        "UPDATE chats SET title = ? WHERE id = ? AND title = 'New chat'",
                        (_title_from(text), chat_id),
                    )
            self._db.execute("UPDATE chats SET updated = ? WHERE id = ?", (now, chat_id))
            self._db.commit()
            message_id = cur.lastrowid
        return self.get_message(message_id)  # type: ignore[return-value]

    def update_message(
        self,
        message_id: int,
        *,
        text: Optional[str] = None,
        image: Optional[str] = None,
        status: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> Optional[dict]:
        fields, values = [], []
        for name, value in (("text", text), ("image", image), ("status", status),
                            ("meta", json.dumps(meta) if meta is not None else None)):
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(value)
        if not fields:
            return self.get_message(message_id)
        with self._lock:
            self._db.execute(
                f"UPDATE messages SET {', '.join(fields)} WHERE id = ?",
                (*values, message_id),
            )
            self._db.commit()
        return self.get_message(message_id)

    def get_message(self, message_id: int) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT id, chat_id, role, text, image, status, created, meta FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        return _with_meta(row) if row else None

    def messages(self, chat_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, chat_id, role, text, image, status, created, meta FROM messages"
                " WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()
        return [_with_meta(r) for r in rows]

    def pending_message(self) -> Optional[dict]:
        """The assistant reply currently being generated, if any."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, chat_id, role, text, image, status, created, meta FROM messages"
                " WHERE status = 'pending' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _with_meta(row) if row else None

    def fail_stale_pending(self, reason: str) -> None:
        """Close replies left pending by a previous run that ended mid-answer."""
        with self._lock:
            self._db.execute(
                "UPDATE messages SET status = 'error', text = ? WHERE status = 'pending'",
                (reason,),
            )
            self._db.commit()

    def conversation_for_restore(self, chat_id: str) -> list[dict]:
        """User/assistant text turns in the shape the daemon restore expects."""
        return [
            {"role": m["role"], "content": m["text"]}
            for m in self.messages(chat_id)
            if m["role"] in ("user", "assistant") and m["status"] == "done" and m["text"]
        ]

    # --- Images ----------------------------------------------------------

    def list_images(self) -> list[dict]:
        """Every generated image, newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, chat_id, text, image, created FROM messages"
                " WHERE image IS NOT NULL AND status = 'done' ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def assistant_messages_with_code(self) -> list[dict]:
        """Finished assistant replies that contain a code block, newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT m.id, m.chat_id, m.text, m.created, c.title AS chat_title"
                " FROM messages m JOIN chats c ON c.id = m.chat_id"
                " WHERE m.role = 'assistant' AND m.status = 'done' AND m.text LIKE '%```%'"
                " ORDER BY m.id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def save_image(self, data: bytes, suffix: str = ".jpg") -> str:
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}{suffix}"
        (self.images_dir / name).write_bytes(data)
        return name

    def image_path(self, name: str) -> Optional[Path]:
        """Resolve a stored image name, refusing anything outside images/."""
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        path = self.images_dir / name
        return path if path.is_file() else None
