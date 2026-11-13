"""Remote control for Volt Code (/rc): show this terminal session in the Volt app.

Volt Code signs in to the Volt server once with the phone password (like a
phone does) and keeps the device token in ~/.config/volt-ai/volt-code-remote.json
(0600). While /rc is on, everything that happens is sent to the app as events,
and messages, approval answers and Stop from the app come back through a long-
polled inbox. The terminal keeps working at the same time.
"""

from __future__ import annotations

import json
import os
import platform
import queue
import threading
import time
from pathlib import Path
from typing import Optional

COOKIE = "volt_session"
DEFAULT_URL = "http://127.0.0.1:8765"


class RemoteError(Exception):
    pass


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "volt-ai" / "volt-code-remote.json"


def load_config() -> Optional[dict]:
    try:
        data = json.loads(config_path().read_text())
        return data if data.get("url") and data.get("token") else None
    except (OSError, ValueError):
        return None


def save_config(url: str, token: str) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"url": url.rstrip("/"), "token": token}, fh)
    tmp.replace(path)


def pair(url: str, password: str, http=None) -> str:
    """Sign in with the phone password; returns the device token."""
    if http is None:
        import requests

        http = requests.Session()
    try:
        resp = http.post(f"{url.rstrip('/')}/api/pair", json={"password": password}, timeout=20)
    except Exception as exc:
        raise RemoteError(f"Can't reach Volt at {url}. Is it running?") from exc
    if resp.status_code != 200:
        try:
            message = resp.json().get("error", "")
        except ValueError:
            message = ""
        raise RemoteError(message or f"Volt said no ({resp.status_code}).")
    token = resp.cookies.get(COOKIE)
    if not token:
        raise RemoteError("Volt didn't send a sign-in token.")
    return token


class RemoteLink:
    """One shared session: sends events, receives app input into ``inbox``."""

    def __init__(self, url: str, token: str, title: str, folder: str, http=None):
        if http is None:
            import requests

            http = requests.Session()
        self.http, self.url = http, url.rstrip("/")
        self.http.cookies.set(COOKIE, token)
        try:
            resp = self.http.post(f"{self.url}/api/rc/sessions", timeout=15, json={
                "title": title, "folder": folder, "host": platform.node()})
        except Exception as exc:
            raise RemoteError(f"Can't reach Volt at {self.url}. Is it running?") from exc
        if resp.status_code == 401:
            raise RemoteError("Volt no longer knows this terminal. Sign in again.")
        if resp.status_code != 200:
            raise RemoteError(f"Volt said no ({resp.status_code}).")
        self.id = resp.json()["id"]
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self._out: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        threading.Thread(target=self._send_loop, daemon=True).start()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def emit(self, event: dict) -> None:
        self._out.put(event)

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            batch = []
            try:
                batch.append(self._out.get(timeout=0.3))
                while len(batch) < 100:
                    batch.append(self._out.get_nowait())
            except queue.Empty:
                pass
            if batch:
                try:
                    self.http.post(f"{self.url}/api/rc/sessions/{self.id}/events",
                                   json={"events": batch}, timeout=15)
                except Exception:
                    time.sleep(1)  # Volt is restarting or offline; drop this batch

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                resp = self.http.get(f"{self.url}/api/rc/sessions/{self.id}/inbox",
                                     params={"wait": 25}, timeout=40)
                if resp.status_code == 200:
                    for item in resp.json().get("inbox", []):
                        self.inbox.put(item)
                else:
                    time.sleep(3)
            except Exception:
                time.sleep(3)

    def close(self) -> None:
        self.emit({"type": "status", "text": "Remote control ended in the terminal."})
        time.sleep(0.5)
        self._stop.set()
        try:
            self.http.delete(f"{self.url}/api/rc/sessions/{self.id}", timeout=5)
        except Exception:
            pass
