"""
Volt AI web chat server.

Serves the Volt chat app (``volt_web_static/``) and a small JSON API to the
desktop window and, when the user turns on phone access, to phones on the
same Wi-Fi network.

Access control
--------------
* The desktop window gets a secret session token in its start URL.
* Phone access is off by default. When it is on, the server also listens on
  the network and a phone must enter the phone password set in Settings on
  the computer before it receives its own device token (a cookie). The
  password is permanent and stored only as a salted hash.
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

from desktop_app import volt_claude, volt_fast, volt_system
from desktop_app.volt_store import VoltStore

STATIC_DIR = Path(__file__).parent / "volt_web_static"
COOKIE = "volt_session"
IMAGE_API = "https://image.pollinations.ai/prompt/"
DEFAULT_PORT = 8765
PAIR_MAX_FAILURES = 5
PAIR_LOCKOUT_SEC = 300
PAIR_GLOBAL_MAX_FAILURES = 20
PHONE_PASSWORD_MIN = 6
PHONE_PASSWORD_ROUNDS = 200_000

DEFAULT_SETTINGS = {
    "images_enabled": False,
    "phone_access": False,
    "claude_enabled": False,
    "claude_model": volt_claude.DEFAULT_MODEL,
    "system_analysis": False,  # add live RAM/CPU/GPU/disk readings to each message
    "local_mode": "fast",  # "fast": one direct model call; "smart": full pipeline with tools
}
LOCAL_MODES = ("fast", "smart")


class VoltBridge(Protocol):
    """What the web server needs from the running assistant."""

    def is_available(self) -> bool: ...
    def submit(self, text: str) -> bool: ...
    def cancel(self) -> None: ...
    def restore(self, messages: list[dict]) -> None: ...


def tailscale_address() -> Optional[str]:
    """This machine's Tailscale IPv4 address (100.64.0.0/10), if connected."""
    import ipaddress
    try:
        import psutil
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and ipaddress.ip_address(a.address) in ipaddress.ip_network("100.64.0.0/10"):
                    return a.address
    except Exception as exc:
        debug_log(f"tailscale address lookup failed: {exc}", "volt")
    return None


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
        self._claude_client = None  # tests inject a fake client
        self._fast_post = None  # tests inject a fake HTTP post for fast mode
        self._lock = threading.RLock()
        self._settings_path = store.data_dir / "settings.json"
        self._devices_path = store.data_dir / "devices.json"
        self.settings = self._load_json(self._settings_path, dict(DEFAULT_SETTINGS))
        self._devices: dict = self._load_json(self._devices_path, {})
        self.desktop_token = secrets.token_urlsafe(32)
        self._password_path = store.data_dir / "phone_password.json"
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
                if key == "claude_model":
                    if value in volt_claude.MODELS:
                        self.settings[key] = value
                elif key == "local_mode":
                    if value in LOCAL_MODES:
                        self.settings[key] = value
                elif key in DEFAULT_SETTINGS:
                    self.settings[key] = bool(value)
            self._save_json(self._settings_path, self.settings)
            if "phone_access" in changes:
                # Rebind after this response has gone out.
                threading.Timer(0.5, self.restart).start()
        return dict(self.settings)

    # --- Phone password -------------------------------------------------------

    @staticmethod
    def _hash_password(password: str, salt: bytes) -> str:
        import hashlib

        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PHONE_PASSWORD_ROUNDS).hex()

    def has_phone_password(self) -> bool:
        return bool(self._load_json(self._password_path, {}).get("hash"))

    def set_phone_password(self, password: str) -> None:
        if len(password) < PHONE_PASSWORD_MIN:
            raise ValueError(f"Use at least {PHONE_PASSWORD_MIN} characters.")
        salt = secrets.token_bytes(16)
        with self._lock:
            self._save_json(self._password_path,
                            {"salt": salt.hex(), "hash": self._hash_password(password, salt)})
        debug_log("volt phone password set", "volt")

    def check_phone_password(self, password: str) -> bool:
        stored = self._load_json(self._password_path, {})
        if not stored.get("hash") or not password:
            return False
        attempt = self._hash_password(password, bytes.fromhex(stored["salt"]))
        return secrets.compare_digest(attempt, stored["hash"])

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

    def claude_active(self) -> bool:
        return bool(self.settings.get("claude_enabled")) and volt_claude.load_key() is not None

    def brain(self) -> str:
        """Which brain answers the next message: "claude", "fast" or "smart"."""
        if self.claude_active():
            return "claude"
        mode = self.settings.get("local_mode", "fast")
        return mode if mode in LOCAL_MODES else "fast"

    def _send(self, chat_id: str, text: str) -> tuple[dict, int]:
        brain = self.brain()
        if brain in ("claude", "fast"):
            return self._send_streamed(chat_id, text, brain)
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

    def _send_streamed(self, chat_id: str, text: str, brain: str) -> tuple[dict, int]:
        if self.store.pending_message() is not None:
            return {"error": "Volt AI is still answering. Wait for the reply first."}, 409
        if self.store.get_chat(chat_id) is None:
            return {"error": "Chat not found."}, 404
        with self._lock:
            # The local brain's memory no longer matches this chat; reload it next time.
            self._active_chat = None
            user = self.store.add_message(chat_id, "user", text)
            pending = self.store.add_message(chat_id, "assistant", "", status="pending")
        threading.Thread(
            target=self._stream_worker, args=(chat_id, pending["id"], brain), daemon=True
        ).start()
        return {"user": user, "reply": self.store.get_message(pending["id"])}, 200

    def _stream_worker(self, chat_id: str, message_id: int, brain: str) -> None:
        key = volt_claude.load_key() if brain == "claude" else None
        try:
            if brain == "claude" and not key:
                raise RuntimeError("no key")
            last = [0.0]

            def on_text(text: str) -> None:
                now = time.monotonic()
                if now - last[0] >= 0.25:  # don't hammer the database
                    last[0] = now
                    current = self.store.get_message(message_id)
                    if current and current["status"] == "pending":
                        self.store.update_message(message_id, text=text, status="pending")

            def should_stop() -> bool:
                current = self.store.get_message(message_id)
                return not current or current["status"] != "pending"

            turns = self.store.conversation_for_restore(chat_id)
            extra, question = "", (turns[-1].get("content", "") if turns else "")
            if self.settings.get("system_analysis") and (
                    brain == "claude" or volt_system.is_about_computer(question)):
                extra = volt_system.prompt_block("" if brain == "claude" else question)
                if brain == "claude":
                    extra = "\n\n" + extra
            if brain == "claude":
                answer = volt_claude.reply(
                    turns,
                    self.settings.get("claude_model", volt_claude.DEFAULT_MODEL),
                    key,
                    client=self._claude_client,
                    on_text=on_text,
                    should_stop=should_stop,
                    extra_system=extra,
                )
            else:
                answer = volt_fast.reply(turns, on_text=on_text, should_stop=should_stop,
                                         post=self._fast_post, extra_system=extra)
            status = "done"
        except volt_claude.Stopped:
            return  # the Stop handler already saved what was written
        except Exception as exc:
            debug_log(f"volt {brain} request failed: {type(exc).__name__}", "volt")
            if brain == "fast":
                answer = volt_fast.friendly_error(exc)
            else:
                answer = (volt_claude.friendly_error(exc) if key
                          else "Add your Claude API key in Settings first.")
            status = "error"
        current = self.store.get_message(message_id)
        if current and current["status"] == "pending":  # not stopped meanwhile
            self.store.update_message(message_id, text=answer, status=status)
            if status == "done":
                self.schedule_backup()

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

        forwarded_headers = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "X-Forwarded-Host")

        def via_tunnel() -> bool:
            return any(h in request.headers for h in forwarded_headers)

        def is_local() -> bool:
            # Tunnel traffic arrives from 127.0.0.1 too, so forwarding headers
            # mark it as remote: only the laptop itself gets laptop powers.
            return request.remote_addr in ("127.0.0.1", "::1") and not via_tunnel()

        def client_ip() -> str:
            if via_tunnel():
                return request.headers.get("Cf-Connecting-Ip") or request.headers.get(
                    "X-Forwarded-For", "?").split(",")[0].strip()
            return request.remote_addr or "?"

        def secure_cookie() -> bool:
            return request.is_secure or request.headers.get("X-Forwarded-Proto") == "https" or via_tunnel()

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
            ip = client_ip()
            now = time.time()
            recent = [t for t in server._pair_failures.get(ip, []) if now - t < PAIR_LOCKOUT_SEC]
            overall = [t for times in server._pair_failures.values() for t in times
                       if now - t < PAIR_LOCKOUT_SEC]
            if len(recent) >= PAIR_MAX_FAILURES or len(overall) >= PAIR_GLOBAL_MAX_FAILURES:
                return jsonify(error="Too many wrong passwords. Wait 5 minutes."), 429
            if not server.has_phone_password():
                return jsonify(error="Set a phone password in Volt AI's Settings on the computer first."), 409
            password = str((request.get_json(silent=True) or {}).get("password", ""))
            if not server.check_phone_password(password):
                recent.append(now)
                server._pair_failures[ip] = recent
                return jsonify(error="That password is not right."), 403
            server._pair_failures.pop(ip, None)
            token = secrets.token_urlsafe(32)
            agent = request.headers.get("User-Agent", "")[:120]
            with server._lock:
                server._devices[token] = {"paired": now, "agent": agent}
                server._save_json(server._devices_path, server._devices)
            debug_log("volt phone paired", "volt")
            resp = jsonify(ok=True)
            resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict",
                            secure=secure_cookie(), max_age=10 * 365 * 24 * 3600)
            return resp

        @app.get("/api/state")
        def state():
            require_auth()
            return jsonify(
                available=server.bridge.is_available(),
                settings=server.settings,
                local=is_local(),
                phone_url=server.phone_url(),
                tailscale_url=server.tailscale_url(),
                phone_password_set=server.has_phone_password() if is_local() else None,
                devices=len(server._devices),
                brain=server.brain(),
                claude={
                    "active": server.claude_active(),
                    "has_key": volt_claude.load_key() is not None,
                    "key_hint": volt_claude.masked_key() if is_local() else None,
                    "models": volt_claude.MODELS,
                },
            )

        @app.get("/api/system")
        def system_info():
            require_auth()
            if not server.settings.get("system_analysis"):
                return jsonify(error="System analyzing is off."), 403
            return jsonify(volt_system.snapshot())

        @app.post("/api/phone-password")
        def phone_password():
            require_auth()
            if not is_local():
                abort(403)  # only the computer itself can set the password
            password = str((request.get_json(silent=True) or {}).get("password", ""))
            try:
                server.set_phone_password(password)
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
            return jsonify(ok=True)

        @app.post("/api/settings")
        def settings():
            require_auth()
            if not is_local():
                abort(403)  # only the laptop can change settings
            changes = request.get_json(silent=True) or {}
            return jsonify(settings=server.update_settings(**changes))

        @app.post("/api/brain")
        def brain():
            require_auth()
            want = str((request.get_json(silent=True) or {}).get("brain", ""))
            if want == "claude":
                if volt_claude.load_key() is None:
                    return jsonify(error="Add your Claude API key in Settings on the laptop first."), 400
                server.update_settings(claude_enabled=True)
            elif want in LOCAL_MODES:
                server.update_settings(claude_enabled=False, local_mode=want)
            else:
                return jsonify(error="Unknown brain."), 400
            return jsonify(brain=server.brain(), active=server.claude_active())

        @app.post("/api/claude/key")
        def claude_key():
            require_auth()
            if not is_local():
                abort(403)
            key = str((request.get_json(silent=True) or {}).get("key", "")).strip()
            if not key:
                volt_claude.clear_key()
                server.update_settings(claude_enabled=False)
                return jsonify(ok=True, has_key=False)
            if not key.startswith("sk-ant-") or len(key) < 30 or any(c.isspace() for c in key):
                return jsonify(error="That doesn't look like a Claude API key. It starts with sk-ant-."), 400
            volt_claude.save_key(key)
            server.update_settings(claude_enabled=True)
            return jsonify(ok=True, has_key=True, key_hint=volt_claude.masked_key())

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
                partial = (pending.get("text") or "").strip()
                if partial:
                    server.store.update_message(pending["id"], text=partial + "\n\n_(Stopped.)_",
                                                status="done")
                else:
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

    def tailscale_url(self) -> Optional[str]:
        """Address that reaches this laptop from the user's other Tailscale devices."""
        if not self.settings.get("phone_access"):
            return None
        addr = tailscale_address()
        return f"http://{addr}:{self.port}/" if addr else None

    def desktop_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.desktop_token}"

    def start(self) -> None:
        from werkzeug.serving import make_server

        host = "0.0.0.0" if self.settings.get("phone_access") else "127.0.0.1"
        if self.brain() == "fast":
            # Load the model now so the first message doesn't wait for it.
            threading.Thread(target=volt_fast.warm_up, daemon=True).start()
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
