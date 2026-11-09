"""
Volt AI web chat server.

Serves the Volt chat app (``volt_web_static/``) and a small JSON API to the
desktop window and, when the user turns on phone access, to phones on the
same Wi-Fi network.

Access control
--------------
* The desktop window gets a secret session token in its start URL.
* Phone access is off by default. When it is on, the server also listens on
  the local network and a phone must enter the 6-digit pairing code shown
  on the laptop before it receives its own device token (a cookie).
* Every API call and image needs a valid token; failed pairing attempts are
  rate-limited.

The AI itself is reached through a *bridge* supplied by the desktop app
(see ``VoltBridge``) so this module never talks to the daemon directly.
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Optional, Protocol

from jarvis.debug import debug_log

from desktop_app.volt_store import VoltStore

STATIC_DIR = Path(__file__).parent / "volt_web_static"
COOKIE = "volt_session"
IMAGE_API = "https://image.pollinations.ai/prompt/"
DEFAULT_PORT = 8765
PAIR_MAX_FAILURES = 5
PAIR_LOCKOUT_SEC = 300

DEFAULT_SETTINGS = {
    "images_enabled": False,
    "phone_access": False,
}


class VoltBridge(Protocol):
    """What the web server needs from the running assistant."""

    def is_available(self) -> bool: ...
    def submit(self, text: str) -> bool: ...
    def cancel(self) -> None: ...
    def restore(self, messages: list[dict]) -> None: ...


def lan_address() -> Optional[str]:
    """Best-effort local network IPv4 address of this machine."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET, nothing is sent
            return s.getsockname()[0]
    except OSError:
        return None


class VoltWebServer:
    def __init__(
        self,
        store: VoltStore,
        bridge: VoltBridge,
        *,
        port: int = DEFAULT_PORT,
        http_get: Optional[Callable] = None,
    ) -> None:
        self.store = store
        self.bridge = bridge
        self.port = port
        self._http_get = http_get
        self._lock = threading.RLock()
        self._settings_path = store.data_dir / "settings.json"
        self._devices_path = store.data_dir / "devices.json"
        self.settings = self._load_json(self._settings_path, dict(DEFAULT_SETTINGS))
        self._devices: dict = self._load_json(self._devices_path, {})
        self.desktop_token = secrets.token_urlsafe(32)
        self.pairing_code = f"{secrets.randbelow(10**6):06d}"
        self._pair_failures: dict[str, list[float]] = {}
        self._active_chat: Optional[str] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._backup_status = {"state": "idle", "message": "", "last": None}
        self._backup_timer: Optional[threading.Timer] = None
        self.store.fail_stale_pending("Volt AI was restarted before this reply finished.")
        self.app = self._build_app()

    # --- Settings and devices ---------------------------------------------

    @staticmethod
    def _load_json(path: Path, default):
        try:
            data = json.loads(path.read_text())
            if isinstance(default, dict) and isinstance(data, dict):
                return {**default, **data}
            return data
        except (OSError, ValueError):
            return default

    def _save_json(self, path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.chmod(0o600)
        tmp.replace(path)

    def update_settings(self, **changes) -> dict:
        with self._lock:
            for key, value in changes.items():
                if key in DEFAULT_SETTINGS:
                    self.settings[key] = bool(value)
            self._save_json(self._settings_path, self.settings)
            if "phone_access" in changes:
                # Rebind after this response has gone out.
                threading.Timer(0.5, self.restart).start()
        return dict(self.settings)

    def _token_ok(self, token: Optional[str]) -> bool:
        if not token:
            return False
        if secrets.compare_digest(token, self.desktop_token):
            return True
        return token in self._devices

    # --- Assistant replies --------------------------------------------------

    def on_reply(self, reply: Optional[str]) -> None:
        """Called by the desktop app when the assistant finishes a chat reply."""
        pending = self.store.pending_message()
        if pending is None:
            return
        self.schedule_backup()
        if reply:
            self.store.update_message(pending["id"], text=reply, status="done")
        else:
            self.store.update_message(
                pending["id"], text="No reply this time. Please try again.", status="error"
            )

    def on_busy(self) -> None:
        pending = self.store.pending_message()
        if pending is not None:
            self.store.update_message(
                pending["id"], text="Volt AI is busy with another question. Try again in a moment.",
                status="error",
            )

    def _send(self, chat_id: str, text: str) -> tuple[dict, int]:
        if not self.bridge.is_available():
            return {"error": "Volt AI is starting or stopped. Try again in a moment."}, 503
        if self.store.pending_message() is not None:
            return {"error": "Volt AI is still answering. Wait for the reply first."}, 409
        if self.store.get_chat(chat_id) is None:
            return {"error": "Chat not found."}, 404
        with self._lock:
            if self._active_chat != chat_id:
                # Load this chat's history into the assistant's memory so it
                # continues the conversation the user is looking at.
                self.bridge.restore(self.store.conversation_for_restore(chat_id))
                self._active_chat = chat_id
            user = self.store.add_message(chat_id, "user", text)
            pending = self.store.add_message(chat_id, "assistant", "", status="pending")
        if not self.bridge.submit(text):
            self.store.update_message(pending["id"], text="Could not reach Volt AI.", status="error")
        debug_log(f"volt chat message submitted to chat {chat_id}", "volt")
        return {"user": user, "reply": self.store.get_message(pending["id"])}, 200

    # --- Images ---------------------------------------------------------------

    def _generate_image(self, chat_id: str, prompt: str) -> tuple[dict, int]:
        if not self.settings.get("images_enabled"):
            return {"error": "Image creation is off. Turn it on in Settings."}, 403
        if self.store.get_chat(chat_id) is None:
            return {"error": "Chat not found."}, 404
        self.store.add_message(chat_id, "user", f"🎨 {prompt}")
        pending = self.store.add_message(chat_id, "assistant", "", status="pending_image")
        threading.Thread(
            target=self._image_worker, args=(pending["id"], prompt), daemon=True
        ).start()
        return {"reply": pending}, 200

    def _image_worker(self, message_id: int, prompt: str) -> None:
        url = IMAGE_API + urllib.parse.quote(prompt, safe="") + "?width=1024&height=1024&nologo=true"
        try:
            get = self._http_get
            if get is None:
                import requests
                get = requests.get
            resp = get(url, timeout=180)
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "")
            if not ctype.startswith("image/"):
                raise ValueError(f"unexpected content type {ctype!r}")
            suffix = ".png" if "png" in ctype else ".jpg"
            name = self.store.save_image(resp.content, suffix)
            self.store.update_message(message_id, text=prompt, image=name, status="done")
            self.schedule_backup()
            debug_log(f"volt image saved as {name}", "volt")
        except Exception as exc:
            debug_log(f"volt image generation failed: {exc}", "volt")
            self.store.update_message(
                message_id, text="Couldn't create that image. Check your internet and try again.",
                status="error",
            )

    # --- Google Drive backup -------------------------------------------------

    def account_email(self) -> Optional[str]:
        try:
            from desktop_app import volt_drive_backup as drive
            return drive.signed_in_email()
        except Exception as exc:
            debug_log(f"volt account lookup failed: {exc}", "volt")
            return None

    def schedule_backup(self, delay: float = 60.0) -> None:
        """Back up shortly after activity, coalescing bursts into one upload."""
        if not self.account_email():
            return
        with self._lock:
            if self._backup_timer is not None:
                self._backup_timer.cancel()
            self._backup_timer = threading.Timer(delay, self.backup_now)
            self._backup_timer.daemon = True
            self._backup_timer.start()

    def backup_now(self) -> None:
        from desktop_app import volt_drive_backup as drive
        self._backup_status.update(state="working", message="Backing up…")
        try:
            info = drive.backup_now(self.store.data_dir)
            self._backup_status.update(state="idle", message="Backed up", last=info.get("modifiedTime"))
        except Exception as exc:
            debug_log(f"volt backup failed: {exc}", "volt")
            self._backup_status.update(state="error", message=f"Backup failed: {exc}")

    def restore_now(self) -> None:
        from desktop_app import volt_drive_backup as drive
        from desktop_app.volt_store import VoltStore
        self._backup_status.update(state="working", message="Restoring your chats…")
        data_dir = self.store.data_dir
        try:
            with self._lock:
                self.store.close()
                try:
                    drive.restore_latest(data_dir)
                finally:
                    self.store = VoltStore(data_dir)
                    self.settings = self._load_json(self._settings_path, dict(DEFAULT_SETTINGS))
                    self._devices = self._load_json(self._devices_path, {})
                    self._active_chat = None
            self._backup_status.update(state="idle", message="Restored from Google Drive")
        except Exception as exc:
            debug_log(f"volt restore failed: {exc}", "volt")
            self._backup_status.update(state="error", message=f"Restore failed: {exc}")

    def sign_in(self) -> None:
        from desktop_app import volt_drive_backup as drive
        self._backup_status.update(state="working", message="Finish signing in in your browser…")
        try:
            email = drive.sign_in()
            self._backup_status.update(state="idle", message=f"Signed in as {email}")
            info = drive.last_backup_info()
            if info and not self.store.list_chats():
                # Fresh install with a backup waiting: bring the chats back.
                self.restore_now()
            else:
                self.backup_now()
        except Exception as exc:
            debug_log(f"volt sign-in failed: {exc}", "volt")
            self._backup_status.update(state="error", message=str(exc))

    # --- Flask app ------------------------------------------------------------

    def _build_app(self):
        from flask import Flask, abort, jsonify, make_response, redirect, request, send_file

        app = Flask(__name__, static_folder=None)
        server = self

        def current_token() -> Optional[str]:
            return request.cookies.get(COOKIE)

        def require_auth():
            if not server._token_ok(current_token()):
                abort(401)

        def is_local() -> bool:
            return request.remote_addr in ("127.0.0.1", "::1")

        @app.after_request
        def _headers(resp):
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["Referrer-Policy"] = "no-referrer"
            resp.headers["X-Frame-Options"] = "DENY"
            return resp

        @app.get("/")
        def index():
            token = request.args.get("t")
            if token and server._token_ok(token):
                resp = make_response(redirect("/"))
                resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict",
                                max_age=10 * 365 * 24 * 3600)
                return resp
            if not server._token_ok(current_token()):
                return send_file(STATIC_DIR / "pair.html")
            return send_file(STATIC_DIR / "index.html")

        @app.get("/static/<path:name>")
        def static_file(name):
            path = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() not in path.parents or not path.is_file():
                abort(404)
            return send_file(path)

        @app.get("/manifest.webmanifest")
        def manifest():
            return send_file(STATIC_DIR / "manifest.webmanifest",
                             mimetype="application/manifest+json")

        @app.post("/api/pair")
        def pair():
            ip = request.remote_addr or "?"
            now = time.time()
            recent = [t for t in server._pair_failures.get(ip, []) if now - t < PAIR_LOCKOUT_SEC]
            if len(recent) >= PAIR_MAX_FAILURES:
                return jsonify(error="Too many wrong codes. Wait 5 minutes."), 429
            code = str((request.get_json(silent=True) or {}).get("code", "")).strip()
            if not secrets.compare_digest(code, server.pairing_code):
                recent.append(now)
                server._pair_failures[ip] = recent
                return jsonify(error="That code is not right. Check the code on your laptop."), 403
            server._pair_failures.pop(ip, None)
            token = secrets.token_urlsafe(32)
            agent = request.headers.get("User-Agent", "")[:120]
            with server._lock:
                server._devices[token] = {"paired": now, "agent": agent}
                server._save_json(server._devices_path, server._devices)
            # One code per phone: show a fresh one for the next device.
            server.pairing_code = f"{secrets.randbelow(10**6):06d}"
            debug_log("volt phone paired", "volt")
            resp = jsonify(ok=True)
            resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict",
                            max_age=10 * 365 * 24 * 3600)
            return resp

        @app.get("/api/state")
        def state():
            require_auth()
            return jsonify(
                available=server.bridge.is_available(),
                settings=server.settings,
                local=is_local(),
                phone_url=server.phone_url(),
                pairing_code=server.pairing_code if is_local() else None,
                devices=len(server._devices),
            )

        @app.post("/api/settings")
        def settings():
            require_auth()
            if not is_local():
                abort(403)  # only the laptop can change settings
            changes = request.get_json(silent=True) or {}
            return jsonify(settings=server.update_settings(**changes))

        @app.post("/api/devices/forget")
        def forget_devices():
            require_auth()
            if not is_local():
                abort(403)
            with server._lock:
                server._devices = {}
                server._save_json(server._devices_path, server._devices)
            return jsonify(ok=True)

        def laptop_only():
            require_auth()
            if not is_local():
                abort(403)

        def run_bg(fn):
            threading.Thread(target=fn, daemon=True).start()
            return jsonify(ok=True)

        @app.get("/api/account")
        def account():
            require_auth()
            return jsonify(email=server.account_email(), backup=server._backup_status)

        @app.post("/api/account/sign-in")
        def account_sign_in():
            laptop_only()
            return run_bg(server.sign_in)

        @app.post("/api/account/sign-out")
        def account_sign_out():
            laptop_only()
            from desktop_app import volt_drive_backup as drive
            drive.sign_out()
            server._backup_status.update(state="idle", message="Signed out", last=None)
            return jsonify(ok=True)

        @app.post("/api/backup")
        def backup():
            laptop_only()
            return run_bg(server.backup_now)

        @app.post("/api/restore")
        def restore():
            laptop_only()
            return run_bg(server.restore_now)

        @app.get("/api/chats")
        def chats():
            require_auth()
            return jsonify(chats=server.store.list_chats())

        @app.post("/api/chats")
        def new_chat():
            require_auth()
            return jsonify(chat=server.store.create_chat())

        @app.get("/api/chats/<chat_id>")
        def chat(chat_id):
            require_auth()
            info = server.store.get_chat(chat_id)
            if info is None:
                abort(404)
            return jsonify(chat=info, messages=server.store.messages(chat_id))

        @app.patch("/api/chats/<chat_id>")
        def rename(chat_id):
            require_auth()
            title = str((request.get_json(silent=True) or {}).get("title", "")).strip()
            if not title or not server.store.rename_chat(chat_id, title):
                abort(400)
            return jsonify(chat=server.store.get_chat(chat_id))

        @app.delete("/api/chats/<chat_id>")
        def delete(chat_id):
            require_auth()
            if server._active_chat == chat_id:
                server._active_chat = None
            return jsonify(ok=server.store.delete_chat(chat_id))

        @app.post("/api/chats/<chat_id>/messages")
        def send(chat_id):
            require_auth()
            text = str((request.get_json(silent=True) or {}).get("text", "")).strip()
            if not text:
                return jsonify(error="Type a message first."), 400
            body, status = server._send(chat_id, text[:20000])
            return jsonify(body), status

        @app.post("/api/chats/<chat_id>/images")
        def image(chat_id):
            require_auth()
            prompt = str((request.get_json(silent=True) or {}).get("prompt", "")).strip()
            if not prompt:
                return jsonify(error="Describe the image first."), 400
            body, status = server._generate_image(chat_id, prompt[:1000])
            return jsonify(body), status

        @app.post("/api/stop")
        def stop():
            require_auth()
            server.bridge.cancel()
            pending = server.store.pending_message()
            if pending is not None and pending["status"] == "pending":
                server.store.update_message(pending["id"], text="Stopped.", status="error")
            return jsonify(ok=True)

        @app.get("/api/images")
        def images():
            require_auth()
            return jsonify(images=server.store.list_images())

        @app.get("/images/<name>")
        def image_file(name):
            require_auth()
            path = server.store.image_path(name)
            if path is None:
                abort(404)
            return send_file(path, as_attachment=request.args.get("download") == "1")

        return app

    # --- Lifecycle --------------------------------------------------------------

    def phone_url(self) -> Optional[str]:
        if not self.settings.get("phone_access"):
            return None
        addr = lan_address()
        return f"http://{addr}:{self.port}/" if addr else None

    def desktop_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.desktop_token}"

    def start(self) -> None:
        from werkzeug.serving import make_server

        host = "0.0.0.0" if self.settings.get("phone_access") else "127.0.0.1"
        for port in (self.port, *range(self.port + 1, self.port + 20)):
            try:
                self._server = make_server(host, port, self.app, threaded=True)
                self.port = port
                break
            except OSError:
                continue
        if self._server is None:
            raise OSError("no free port for the Volt AI web chat")
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        print(f"🌐 Volt AI chat server on {host}:{self.port}", flush=True)
        debug_log(f"volt web server listening on {host}:{self.port}", "volt")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def restart(self) -> None:
        self.stop()
        self.start()
