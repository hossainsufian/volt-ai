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

Artifact mode
-------------
A message sent with ``artifact: true`` asks the model for one self-contained
HTML file (``volt_fast.ARTIFACT_PROMPT``). It always goes to a direct brain
(Fast or Claude), never the assistant pipeline. The page previews it in a
sandboxed frame with an opaque origin, so artifact code can't reach Volt's
API or cookies.

The AI itself is reached through a *bridge* supplied by the desktop app
(see ``VoltBridge``) so this module never talks to the daemon directly.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Optional, Protocol

from jarvis.debug import debug_log

import volt_brand
from desktop_app import volt_claude, volt_fast, volt_images, volt_online, volt_rc, volt_system
from desktop_app.volt_store import VoltStore

STATIC_DIR = Path(__file__).parent / "volt_web_static"
COOKIE = "volt_session"
IMAGE_RETRY_CAP_SEC = 60  # longest wait for the free image service before trying again
IMAGE_MAX_COUNT = 4
ATTACH_MAX_COUNT = 6
ATTACH_MAX_BYTES = 10 * 1024 * 1024
ATTACH_TEXT_CHARS = 100_000
PICTURE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
TEXT_SUFFIXES = (".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".json", ".csv", ".xml", ".yaml",
                 ".yml", ".toml", ".ini", ".log", ".sh", ".ps1", ".java", ".c", ".cpp", ".h", ".go", ".rs",
                 ".php", ".rb", ".sql", ".kt", ".swift", ".tsx", ".jsx", ".svg")
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
    "online_enabled": False,  # answer with the online brain (Gemini) when a key is saved
    "claude_model": volt_claude.DEFAULT_MODEL,
    "system_analysis": False,  # add live RAM/CPU/GPU/disk readings to each message
    "image_model": "",  # Pollinations model when a key is saved; empty = the default
    "fast_model": "",  # Ollama model for Fast mode; empty = the assistant's chat model
    "local_mode": "fast",  # "fast": one direct model call; "smart": full pipeline with tools
}
LOCAL_MODES = ("fast", "smart")


ARTIFACT_BLOCK = re.compile(r"```(html|svg)[ \t]*\n(.*?)```", re.IGNORECASE | re.DOTALL)
ARTIFACT_TITLE = re.compile(r"<title>([^<]{1,80})</title>", re.IGNORECASE)


def find_artifacts(text: str) -> list[tuple[str, str]]:
    """(lang, code) for each web page or SVG picture in a reply."""
    return [(m.group(1).lower(), m.group(2)) for m in ARTIFACT_BLOCK.finditer(text or "")]


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
        self._online_post = None  # tests inject a fake HTTP post for the online brain
        self._image_get = None  # tests inject fakes for the image service
        self._image_post = None
        self._enhance = None  # tests inject a fake prompt improver
        self.rc = volt_rc.RemoteSessions()  # Volt Code terminals shared with the app (/rc)
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
                elif key == "image_model":
                    self.settings[key] = value if value in volt_images.MODELS else ""
                elif key == "fast_model":
                    self.settings[key] = str(value or "").strip()[:100]
                    threading.Thread(target=volt_fast.warm_up, daemon=True,
                                     kwargs={"model": self.settings[key] or None}).start()
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

    def write_local_link(self) -> None:
        """Leave Volt Code on this computer a private key (0600) so /rc connects with no
        questions, like Claude Code's remote control. Only this user account can read it."""
        path = self.store.data_dir / "volt-code-link.json"
        token = None
        try:
            old = json.loads(path.read_text()).get("token")
            if old in self._devices:
                token = old
        except (OSError, ValueError):
            pass
        with self._lock:
            if token is None:
                token = secrets.token_urlsafe(32)
                self._devices[token] = {"paired": time.time(), "agent": "Volt Code on this computer"}
                self._save_json(self._devices_path, self._devices)
            self._save_json(path, {"url": f"http://127.0.0.1:{self.port}", "token": token})

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

    def online_active(self) -> bool:
        return bool(self.settings.get("online_enabled")) and volt_online.load() is not None

    def brain(self) -> str:
        """Which brain answers the next message: "claude", "online", "fast" or "smart"."""
        if self.claude_active():
            return "claude"
        if self.online_active():
            return "online"
        mode = self.settings.get("local_mode", "fast")
        return mode if mode in LOCAL_MODES else "fast"

    def _read_attachments(self, items) -> tuple[Optional[dict], Optional[str]]:
        """Check pasted pictures and files; returns (meta for the message, error)."""
        import base64

        if not items:
            return None, None
        if not isinstance(items, list) or len(items) > ATTACH_MAX_COUNT:
            return None, f"Attach at most {ATTACH_MAX_COUNT} files at once."
        pictures, files = [], []
        for item in items:
            name = str((item or {}).get("name") or "file")[:120]
            kind = str(item.get("type") or "").lower()
            try:
                data = base64.b64decode(str(item.get("data") or ""), validate=False)
            except ValueError:
                return None, f"{name} couldn't be read."
            if not data:
                return None, f"{name} is empty."
            if len(data) > ATTACH_MAX_BYTES:
                return None, f"{name} is too big (the limit is {ATTACH_MAX_BYTES // (1024 * 1024)} MB)."
            if kind in PICTURE_TYPES:
                pictures.append({"image": self.store.save_image(data, PICTURE_TYPES[kind]), "mime": kind, "name": name})
            elif kind.startswith("text/") or kind == "application/json" or name.lower().endswith(TEXT_SUFFIXES):
                files.append({"name": name, "text": data.decode("utf-8", errors="replace")[:ATTACH_TEXT_CHARS]})
            else:
                return None, f"{name}: Volt can read pictures and text or code files."
        meta = {}
        if pictures:
            meta["attachments"] = pictures
        if files:
            meta["files"] = files
        return meta, None

    def _send(self, chat_id: str, text: str, artifact: bool = False,
              vector: Optional[str] = None, attachments=None) -> tuple[dict, int]:
        brain = self.brain()
        meta, error = self._read_attachments(attachments)
        if error:
            return {"error": error}, 400
        if meta and meta.get("attachments") and brain not in ("online", "claude"):
            # Only Gemini and Claude can look at pictures.
            if volt_online.load() is not None:
                brain = "online"
            elif volt_claude.load_key() is not None:
                brain = "claude"
            else:
                return {"error": "To look at pictures, Volt needs Gemini (free key: Settings → Gemini brain) "
                                 "or Claude. The brain on this computer can't see images."}, 400
        if meta and brain == "smart":
            brain = "fast"  # attached files go to a direct brain
        if (artifact or vector) and brain == "smart":
            brain = "fast"  # artifacts need the model's raw answer, not the assistant pipeline
        if brain in ("claude", "online", "fast"):
            return self._send_streamed(chat_id, text, brain, artifact, vector, meta)
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

    def _send_streamed(self, chat_id: str, text: str, brain: str, artifact: bool = False,
                       vector: Optional[str] = None, meta: Optional[dict] = None) -> tuple[dict, int]:
        if self.store.pending_message() is not None:
            return {"error": "Volt AI is still answering. Wait for the reply first."}, 409
        if self.store.get_chat(chat_id) is None:
            return {"error": "Chat not found."}, 404
        with self._lock:
            # The local brain's memory no longer matches this chat; reload it next time.
            self._active_chat = None
            user = self.store.add_message(chat_id, "user", text, meta=meta)
            pending = self.store.add_message(chat_id, "assistant", "", status="pending")
        threading.Thread(
            target=self._stream_worker, args=(chat_id, pending["id"], brain, artifact, vector), daemon=True
        ).start()
        return {"user": user, "reply": self.store.get_message(pending["id"])}, 200

    def _answer_again(self, chat_id: str) -> tuple[dict, int]:
        """Answer the chat's last message again (after a retry or an edit)."""
        brain = self.brain()
        if brain == "smart":
            brain = "fast"  # the assistant pipeline keeps its own history; answer directly
        with self._lock:
            self._active_chat = None
            pending = self.store.add_message(chat_id, "assistant", "", status="pending")
        threading.Thread(target=self._stream_worker, args=(chat_id, pending["id"], brain), daemon=True).start()
        return {"reply": pending}, 200

    def _retry(self, message_id: int) -> tuple[dict, int]:
        msg = self.store.get_message(message_id)
        if not msg or msg["role"] != "assistant" or msg.get("image"):
            return {"error": "Only an answer can be retried."}, 400
        if self.store.pending_message() is not None:
            return {"error": "Volt AI is still answering. Wait for the reply first."}, 409
        self.store.delete_messages_from(msg["chat_id"], message_id)
        return self._answer_again(msg["chat_id"])

    def _edit(self, message_id: int, text: str) -> tuple[dict, int]:
        msg = self.store.get_message(message_id)
        if not msg or msg["role"] != "user":
            return {"error": "Only your own messages can be edited."}, 400
        if not text.strip():
            return {"error": "The message can't be empty."}, 400
        if self.store.pending_message() is not None:
            return {"error": "Volt AI is still answering. Wait for the reply first."}, 409
        # Keep the message's id (so it stays in place), drop everything after it.
        self.store.update_message(message_id, text=text.strip()[:20000])
        later = [m["id"] for m in self.store.messages(msg["chat_id"]) if m["id"] > message_id]
        if later:
            self.store.delete_messages_from(msg["chat_id"], later[0])
        return self._answer_again(msg["chat_id"])

    def _stream_worker(self, chat_id: str, message_id: int, brain: str,
                       artifact: bool = False, vector: Optional[str] = None) -> None:
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

            turns = self.store.conversation_with_media(chat_id)
            extra, question = "", (turns[-1].get("content", "") if turns else "")
            cloud = brain in ("claude", "online")  # big models: give them the full readings
            if self.settings.get("system_analysis") and (cloud or volt_system.is_about_computer(question)):
                extra = volt_system.prompt_block("" if cloud else question)
            if vector:
                artifact = True  # same room for a long answer
                prompt = volt_images.DIAGRAM_PROMPT if vector == "diagram" else volt_images.VECTOR_LOGO_PROMPT
                extra = (extra + "\n\n" if extra else "") + prompt
            elif artifact:
                extra = (extra + "\n\n" if extra else "") + volt_fast.ARTIFACT_PROMPT
                if brain == "fast":
                    turns = turns[-volt_fast.ARTIFACT_TURNS:]
            if cloud and extra and not extra.startswith("\n"):
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
            elif brain == "online":
                answer = volt_online.reply(turns, on_text=on_text, should_stop=should_stop,
                                           extra_system=extra, post=self._online_post)
            else:
                answer = volt_fast.reply(turns, on_text=on_text, should_stop=should_stop,
                                         model=self.settings.get("fast_model") or None,
                                         post=self._fast_post, extra_system=extra)
            status = "done"
        except volt_claude.Stopped:
            return  # the Stop handler already saved what was written
        except Exception as exc:
            debug_log(f"volt {brain} request failed: {type(exc).__name__}", "volt")
            if brain == "fast":
                answer = volt_fast.friendly_error(exc)
            elif brain == "online":
                answer = volt_online.friendly_error(exc)
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

    def _generate_image(self, chat_id: str, prompt: str, style: str = "auto", shape: str = "square",
                        count: int = 1, enhance: bool = True, model: Optional[str] = None) -> tuple[dict, int]:
        if not self.settings.get("images_enabled"):
            return {"error": "Image creation is off. Turn it on in Settings."}, 403
        if self.store.get_chat(chat_id) is None:
            return {"error": "Chat not found."}, 404
        if style in volt_images.VECTOR_STYLES:
            if self.claude_active() or volt_online.load() is not None:
                return self._send(chat_id, f"{volt_images.VECTOR_STYLES[style]}: {prompt}", vector=style)
            # The small local model can't draw usable SVG: make a picture instead.
            style = "logo" if style == "vector_logo" else "diagram_picture"
        style = style if style in volt_images.STYLES else "auto"
        shape = shape if shape in volt_images.SHAPES else "square"
        count = max(1, min(IMAGE_MAX_COUNT, int(count or 1)))
        model = model if model in volt_images.MODELS else self.settings.get("image_model") or None
        self.store.add_message(chat_id, "user", f"🎨 {prompt}",
                               meta={"style": style, "shape": shape, "count": count})
        pendings = [self.store.add_message(
            chat_id, "assistant", "", status="pending_image",
            meta={"prompt": prompt, "style": style, "shape": shape, "model": model,
                  "seed": secrets.randbelow(2_000_000_000)}) for _ in range(count)]
        threading.Thread(target=self._image_job, args=([p["id"] for p in pendings], enhance),
                         daemon=True).start()
        return {"reply": pendings[0], "replies": pendings}, 200

    def _improve_prompt(self, idea: str, style: str) -> str:
        """A detailed image description from a short idea (best effort: the idea itself on failure)."""
        if self._enhance is not None:
            return self._enhance(idea, style)
        turns = [{"role": "user", "content": idea}]
        try:
            if volt_online.load() is not None:
                text = volt_online.reply(turns, extra_system="\n\n" + volt_images.ENHANCE_PROMPT)
            else:
                text = volt_fast.reply(turns, extra_system=volt_images.ENHANCE_PROMPT,
                                       model=self.settings.get("fast_model") or None, post=self._fast_post)
        except Exception as exc:
            debug_log(f"volt prompt improver failed: {type(exc).__name__}", "volt")
            return idea
        return volt_images.clean_enhanced(text, idea)

    def _image_job(self, message_ids: list[int], enhance: bool = False, change: Optional[str] = None,
                   source: Optional[str] = None) -> None:
        """Make the pictures for these pending messages, one after another."""
        first = self.store.get_message(message_ids[0])
        meta = dict(first["meta"] or {})
        final = meta.get("final_prompt") or meta.get("prompt", "")
        if enhance and not change:
            final = self._improve_prompt(meta.get("prompt", ""), meta.get("style", "auto"))
        for mid in message_ids:
            msg = self.store.get_message(mid)
            meta = dict(msg["meta"] or {})
            meta["final_prompt"] = final
            try:
                if change and source and volt_images.load_key():
                    data = volt_images.edit(self.store.image_path(source).read_bytes(), change,
                                            post=self._image_post)
                    suffix = ".png" if data[:4] == b"\x89PNG" else ".jpg"
                else:
                    full = volt_images.styled_prompt(final, meta.get("style", "auto"))
                    data, suffix = self._make_picture(full, meta)
                name = self.store.save_image(data, suffix)
                self.store.update_message(mid, text=meta.get("prompt", ""), image=name, status="done", meta=meta)
                debug_log(f"volt image saved as {name}", "volt")
            except volt_images.ImageError as exc:
                self.store.update_message(mid, text=str(exc), status="error", meta=meta)
            except Exception as exc:
                debug_log(f"volt image generation failed: {type(exc).__name__}: {exc}", "volt")
                self.store.update_message(mid, text="Couldn't create that image. Try again.", status="error")
        self.schedule_backup()

    def _make_picture(self, prompt: str, meta: dict) -> tuple[bytes, str]:
        for attempt in range(4):
            try:
                return volt_images.generate(prompt, meta.get("shape", "square"), meta["seed"],
                                            model=meta.get("model"), get=self._image_get)
            except volt_images.RateLimited as exc:
                if attempt == 3:
                    raise
                time.sleep(min(exc.seconds + 1, IMAGE_RETRY_CAP_SEC))
        raise volt_images.ImageError("The image service is busy. Try again in a minute.")

    def _remake_image(self, message_id: int, change: Optional[str] = None) -> tuple[dict, int]:
        msg = self.store.get_message(message_id)
        if not msg or msg["role"] != "assistant" or not msg.get("meta") or not msg.get("image"):
            return {"error": "That picture can't be remade."}, 404
        meta = dict(msg["meta"])
        if change:
            change = change.strip()[:500]
            if not volt_images.load_key():
                # No editing without a key: describe the change and keep the seed (similar layout).
                meta["final_prompt"] = f"{meta.get('final_prompt') or meta.get('prompt', '')}, {change}"
            meta["prompt"] = f"{meta.get('prompt', '')} → {change}"
            self.store.add_message(msg["chat_id"], "user", f"✏️ {change}")
        else:
            meta["seed"] = secrets.randbelow(2_000_000_000)
        pending = self.store.add_message(msg["chat_id"], "assistant", "", status="pending_image", meta=meta)
        threading.Thread(target=self._image_job, args=([pending["id"]],),
                         kwargs={"change": change, "source": msg["image"]}, daemon=True).start()
        return {"reply": pending}, 200

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
        app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024  # several pasted pictures, base64-encoded
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
                fast_models={
                    "installed": volt_fast.installed_models() if is_local() else [],
                    "default": volt_fast.model_and_url()[0],
                },
                about={"text": volt_brand.ABOUT, "based_on": volt_brand.BASED_ON},
                images={
                    "has_key": volt_images.load_key() is not None,
                    "key_hint": volt_images.masked_key() if is_local() else None,
                    "key_page": volt_images.KEY_PAGE,
                    # Lists of [value, label] pairs: JSON objects lose their order here.
                    "models": [[k, v] for k, v in volt_images.MODELS.items()],
                    "styles": [[k, v[0]] for k, v in volt_images.STYLES.items()]
                    + [[k, v] for k, v in volt_images.VECTOR_STYLES.items()],
                    "shapes": [[k, v[0]] for k, v in volt_images.SHAPES.items()],
                },
                online={
                    "active": server.online_active(),
                    "has_key": volt_online.load() is not None,
                    "key_hint": volt_online.masked_key() if is_local() else None,
                    "model": volt_online.model_name(),
                    "provider": volt_online.PROVIDER,
                    "key_page": volt_online.KEY_PAGE,
                },
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
                server.update_settings(claude_enabled=True, online_enabled=False)
            elif want == "online":
                if volt_online.load() is None:
                    return jsonify(error=f"Add your {volt_online.PROVIDER} API key in Settings on the computer first."), 400
                server.update_settings(claude_enabled=False, online_enabled=True)
            elif want in LOCAL_MODES:
                server.update_settings(claude_enabled=False, online_enabled=False, local_mode=want)
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
            server.update_settings(claude_enabled=True, online_enabled=False)
            return jsonify(ok=True, has_key=True, key_hint=volt_claude.masked_key())

        @app.post("/api/online/key")
        def online_key():
            require_auth()
            if not is_local():
                abort(403)  # keys are only entered on the computer itself
            key = str((request.get_json(silent=True) or {}).get("key", "")).strip()
            if not key:
                volt_online.clear()
                server.update_settings(online_enabled=False)
                return jsonify(ok=True, has_key=False)
            if len(key) < 20 or any(c.isspace() for c in key):
                return jsonify(error=f"That doesn't look like a {volt_online.PROVIDER} API key."), 400
            try:
                model = volt_online.check_key(key)
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (400, 401, 403):
                    return jsonify(error=f"{volt_online.PROVIDER} did not accept this key."), 400
                debug_log(f"online key check failed: {type(exc).__name__}", "volt")
                return jsonify(error=f"Couldn't reach {volt_online.PROVIDER} to check the key. Try again."), 502
            volt_online.save(key, model)
            server.update_settings(online_enabled=True, claude_enabled=False)
            debug_log(f"online brain key saved, model {model}", "volt")
            return jsonify(ok=True, has_key=True, key_hint=volt_online.masked_key(), model=model)

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
            data = request.get_json(silent=True) or {}
            text = str(data.get("text", "")).strip()
            attachments = data.get("attachments") or None
            if not text and not attachments:
                return jsonify(error="Type a message first."), 400
            body, status = server._send(chat_id, text[:20000], artifact=bool(data.get("artifact")),
                                        attachments=attachments)
            return jsonify(body), status

        @app.post("/api/chats/<chat_id>/images")
        def image(chat_id):
            require_auth()
            data = request.get_json(silent=True) or {}
            prompt = str(data.get("prompt", "")).strip()
            if not prompt:
                return jsonify(error="Describe the image first."), 400
            try:
                count = int(data.get("count", 1))
            except (TypeError, ValueError):
                count = 1
            body, status = server._generate_image(
                chat_id, prompt[:1000], style=str(data.get("style", "auto")),
                shape=str(data.get("shape", "square")), count=count,
                enhance=bool(data.get("enhance", True)), model=data.get("model"))
            return jsonify(body), status

        @app.post("/api/messages/<int:message_id>/retry")
        def retry(message_id):
            require_auth()
            body, status = server._retry(message_id)
            return jsonify(body), status

        @app.post("/api/messages/<int:message_id>/edit")
        def edit_message(message_id):
            require_auth()
            text = str((request.get_json(silent=True) or {}).get("text", ""))
            body, status = server._edit(message_id, text)
            return jsonify(body), status

        @app.post("/api/images/<int:message_id>/again")
        def image_again(message_id):
            require_auth()
            body, status = server._remake_image(message_id)
            return jsonify(body), status

        @app.post("/api/images/<int:message_id>/change")
        def image_change(message_id):
            require_auth()
            change = str((request.get_json(silent=True) or {}).get("change", "")).strip()
            if not change:
                return jsonify(error="Say what to change."), 400
            body, status = server._remake_image(message_id, change=change)
            return jsonify(body), status

        @app.post("/api/images/key")
        def image_key():
            require_auth()
            if not is_local():
                abort(403)  # keys are only entered on the computer itself
            key = str((request.get_json(silent=True) or {}).get("key", "")).strip()
            if not key:
                volt_images.clear_key()
                return jsonify(ok=True, has_key=False)
            if not key.startswith("sk_") or len(key) < 12 or any(c.isspace() for c in key):
                return jsonify(error="That doesn't look like a Pollinations secret key. It starts with sk_."), 400
            volt_images.save_key(key)
            debug_log("volt pollinations key saved", "volt")
            return jsonify(ok=True, has_key=True, key_hint=volt_images.masked_key())

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

        # --- Volt Code remote control (/rc) -------------------------------------------
        def rc_session(sid):
            if not server.rc.exists(sid):
                abort(404)

        @app.post("/api/rc/sessions")
        def rc_create():
            require_auth()
            data = request.get_json(silent=True) or {}
            sid = server.rc.create(str(data.get("title", "")), str(data.get("folder", "")), str(data.get("host", "")))
            debug_log("volt code session shared with the app", "volt")
            return jsonify(id=sid)

        @app.get("/api/rc/sessions")
        def rc_list():
            require_auth()
            return jsonify(sessions=server.rc.list())

        @app.post("/api/rc/sessions/<sid>/events")
        def rc_events_add(sid):
            require_auth()
            rc_session(sid)
            events = (request.get_json(silent=True) or {}).get("events") or []
            server.rc.add_events(sid, [e for e in events if isinstance(e, dict)][:200])
            return jsonify(ok=True)

        @app.get("/api/rc/sessions/<sid>/events")
        def rc_events(sid):
            require_auth()
            rc_session(sid)
            try:
                after = int(request.args.get("after", 0))
            except ValueError:
                after = 0
            return jsonify(events=server.rc.events(sid, after=after))

        @app.get("/api/rc/sessions/<sid>/inbox")
        def rc_inbox(sid):
            require_auth()
            rc_session(sid)
            try:
                wait = float(request.args.get("wait", 25))
            except ValueError:
                wait = 25
            return jsonify(inbox=server.rc.take_inbox(sid, wait=wait))

        @app.post("/api/rc/sessions/<sid>/input")
        def rc_input(sid):
            require_auth()
            rc_session(sid)
            data = request.get_json(silent=True) or {}
            if data.get("stop"):
                item = {"stop": True}
            elif "approval" in data:
                if data.get("answer") not in ("yes", "always", "no"):
                    return jsonify(error="Answer yes, always or no."), 400
                item = {"approval": str(data["approval"]), "answer": data["answer"]}
            else:
                text = str(data.get("text", "")).strip()
                if not text:
                    return jsonify(error="Type a message first."), 400
                item = {"text": text[:20000]}
            server.rc.send(sid, item)
            return jsonify(ok=True)

        @app.delete("/api/rc/sessions/<sid>")
        def rc_end(sid):
            require_auth()
            rc_session(sid)
            server.rc.end(sid)
            return jsonify(ok=True)

        @app.get("/api/artifacts")
        def artifacts():
            require_auth()
            items = []
            for msg in server.store.assistant_messages_with_code():
                for index, (lang, code) in enumerate(find_artifacts(msg["text"])):
                    title = ARTIFACT_TITLE.search(code)
                    items.append({
                        "message_id": msg["id"], "index": index, "lang": lang,
                        "title": title.group(1).strip() if title else ("Picture" if lang == "svg" else "Web page"),
                        "chat_id": msg["chat_id"], "chat_title": msg["chat_title"],
                        "created": msg["created"], "size": len(code),
                    })
            return jsonify(artifacts=items)

        @app.get("/api/artifacts/<int:message_id>/<int:index>")
        def artifact(message_id, index):
            require_auth()
            msg = server.store.get_message(message_id)
            found = find_artifacts(msg["text"]) if msg and msg["role"] == "assistant" else []
            if index >= len(found):
                abort(404)
            lang, code = found[index]
            return jsonify(lang=lang, code=code)

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
            threading.Thread(target=volt_fast.warm_up, daemon=True,
                             kwargs={"model": self.settings.get("fast_model") or None}).start()
        for port in (self.port, *range(self.port + 1, self.port + 20)):
            try:
                self._server = make_server(host, port, self.app, threaded=True)
                self.port = port
                break
            except OSError:
                continue
        if self._server is None:
            raise OSError("no free port for the Volt AI web chat")
        try:
            self.write_local_link()
        except OSError as exc:
            debug_log(f"volt code link not written: {exc}", "volt")
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
