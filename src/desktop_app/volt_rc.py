"""Remote control for Volt Code (/rc).

A Volt Code terminal session can share itself with the Volt app: it registers
here, sends what happens (text, tool calls, approval requests) as events, and
collects what the user sends from the app (messages, approval answers, stop)
from its inbox by long polling. Everything is kept in memory: sessions end
when Volt restarts or the terminal says goodbye.
"""

from __future__ import annotations

import secrets
import threading
import time

MAX_EVENTS = 2000  # per session; older events are dropped
MAX_SESSIONS = 20


class RemoteSessions:
    def __init__(self, offline_after: float = 90.0):
        self.offline_after = offline_after
        self._cond = threading.Condition()
        self._sessions: dict[str, dict] = {}

    def create(self, title: str, folder: str, host: str) -> str:
        sid = secrets.token_hex(8)
        now = time.time()
        with self._cond:
            if len(self._sessions) >= MAX_SESSIONS:  # forget the oldest
                oldest = min(self._sessions, key=lambda k: self._sessions[k]["seen"])
                del self._sessions[oldest]
            self._sessions[sid] = {
                "id": sid, "title": (title or "Volt Code")[:120], "folder": (folder or "")[:300],
                "host": (host or "")[:80], "created": now, "seen": now, "ended": False,
                "events": [], "seq": 0, "inbox": [],
            }
        return sid

    def exists(self, sid: str) -> bool:
        with self._cond:
            return sid in self._sessions

    def add_events(self, sid: str, events: list[dict]) -> None:
        with self._cond:
            session = self._sessions[sid]
            session["seen"] = time.time()
            for event in events:
                session["seq"] += 1
                session["events"].append({**event, "seq": session["seq"], "at": time.time()})
            del session["events"][:-MAX_EVENTS]
            self._cond.notify_all()

    def events(self, sid: str, after: int = 0) -> list[dict]:
        with self._cond:
            return [e for e in self._sessions[sid]["events"] if e["seq"] > after]

    def send(self, sid: str, item: dict) -> None:
        with self._cond:
            self._sessions[sid]["inbox"].append(item)
            self._cond.notify_all()

    def take_inbox(self, sid: str, wait: float = 25.0) -> list[dict]:
        """What the app sent since last time; waits up to ``wait`` seconds for something."""
        deadline = time.monotonic() + max(0.0, min(wait, 30.0))
        with self._cond:
            session = self._sessions[sid]
            session["seen"] = time.time()
            while not session["inbox"] and not session["ended"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            items, session["inbox"] = session["inbox"], []
            session["seen"] = time.time()
            return items

    def end(self, sid: str) -> None:
        with self._cond:
            if sid in self._sessions:
                self._sessions[sid]["ended"] = True
                self._cond.notify_all()

    def list(self) -> list[dict]:
        now = time.time()
        with self._cond:
            out = [{
                "id": s["id"], "title": s["title"], "folder": s["folder"], "host": s["host"],
                "created": s["created"], "last_event": s["seq"],
                "online": not s["ended"] and now - s["seen"] < self.offline_after,
            } for s in self._sessions.values()]
        return sorted(out, key=lambda s: s["created"], reverse=True)
