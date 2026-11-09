"""Back up and restore Volt AI's local data folder to the user's Google Drive.

The user signs in with their own Google account through the system browser
(OAuth 2.0 for installed apps, loopback redirect with PKCE), so no password is
ever typed into Volt AI. Only the ``drive.file`` scope is requested: Volt AI
can see and change the files it created itself and nothing else in the
user's Drive.

This module is desktop-side only; the core ``jarvis`` package must not import
it. See ``volt_drive_backup.spec.md`` for the full contract.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from jarvis.debug import debug_log

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SCOPES = ("openid", "email", DRIVE_FILE_SCOPE)
SCOPE_STRING = " ".join(SCOPES)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"

FOLDER_MIME = "application/vnd.google-apps.folder"
BACKUP_FOLDER_NAME = "Volt AI Backup"
BACKUP_FILE_NAME = "volt-ai-backup.zip"
ZIP_MIME = "application/zip"

CLIENT_FILE_NAME = "google_oauth_client.json"
TOKEN_FILE_NAME = "google_token.json"

MAX_FILE_BYTES = 200 * 1024 * 1024
RESUMABLE_THRESHOLD_BYTES = 100 * 1024 * 1024
SKIP_SUFFIXES = (".lock", "-wal", "-shm", "-journal")
FILE_FIELDS = "id,name,size,modifiedTime,md5Checksum"

# Refresh a little early so a token never expires mid-request.
EXPIRY_MARGIN_SECONDS = 60
API_TIMEOUT = (10, 60)
TRANSFER_TIMEOUT = (10, 600)

_LOG = "drive"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class VoltDriveError(Exception):
    """Base class for every error raised by this module."""


class MissingOAuthClientError(VoltDriveError):
    """The user's Google OAuth Desktop client file is missing or unusable."""


class OAuthError(VoltDriveError):
    """Sign-in with Google failed or was cancelled."""


class OAuthStateError(OAuthError):
    """The browser callback carried a ``state`` we did not issue (possible CSRF)."""


class NotSignedInError(VoltDriveError):
    """No usable Google sign-in is stored; the user must sign in again."""


class DriveApiError(VoltDriveError):
    """Google Drive returned an error response."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class NoBackupError(VoltDriveError):
    """There is no Volt AI backup in the signed-in user's Drive."""


class UnsafeBackupError(VoltDriveError):
    """The downloaded backup contains entries that would escape the data folder."""


# ---------------------------------------------------------------------------
# Paths and file helpers
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    """Return ``~/.config/volt-ai`` (resolved at call time so tests can move HOME)."""
    return Path.home() / ".config" / "volt-ai"


def client_file_path() -> Path:
    return config_dir() / CLIENT_FILE_NAME


def token_file_path() -> Path:
    return config_dir() / TOKEN_FILE_NAME


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_private_json(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write ``data`` to ``path`` with mode 0600, never world-readable."""
    _ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _missing_client_message(path: Path, detail: str) -> str:
    return (
        f"Google sign-in needs your own OAuth client file at {path} ({detail}). "
        "Create it in Google Cloud Console: enable the Google Drive API, configure the "
        "OAuth consent screen, then Credentials > Create credentials > OAuth client ID > "
        "Application type 'Desktop app', and download the JSON. Save it as "
        f"{path}. It must contain an \"installed\" section with client_id and client_secret."
    )


def load_client_config() -> Dict[str, str]:
    """Read the user's Desktop OAuth client (the ``installed`` JSON from Google)."""
    path = client_file_path()
    if not path.is_file():
        raise MissingOAuthClientError(_missing_client_message(path, "file not found"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MissingOAuthClientError(
            _missing_client_message(path, f"could not be read as JSON: {exc}")) from exc
    installed = raw.get("installed") if isinstance(raw, dict) else None
    if not isinstance(installed, dict):
        raise MissingOAuthClientError(_missing_client_message(
            path, "no \"installed\" section, so it is not a Desktop app client"))
    if not installed.get("client_id") or not installed.get("client_secret"):
        raise MissingOAuthClientError(
            _missing_client_message(path, "client_id or client_secret is missing"))
    return {
        "client_id": installed["client_id"],
        "client_secret": installed["client_secret"],
        "token_uri": installed.get("token_uri") or TOKEN_URL,
    }


def _load_tokens() -> Optional[Dict[str, Any]]:
    path = token_file_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        debug_log(f"token file unreadable, treating as signed out: {exc}", _LOG)
        return None
    return data if isinstance(data, dict) and data.get("refresh_token") else None


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

_default_session: Optional[requests.Session] = None
_session_lock = threading.Lock()


def _http(session: Optional[Any]) -> Any:
    """Return the injected session or a shared module-level ``requests.Session``."""
    global _default_session
    if session is not None:
        return session
    with _session_lock:
        if _default_session is None:
            _default_session = requests.Session()
        return _default_session


def _error_text(resp: Any) -> str:
    try:
        body = resp.json()
    except Exception:
        return (getattr(resp, "text", "") or "")[:300]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if err:
            return f"{err}: {body.get('error_description', '')}".strip(": ")
    return str(body)[:300]


def _check(resp: Any, what: str) -> Any:
    if not (200 <= resp.status_code < 300):
        raise DriveApiError(f"{what} failed (HTTP {resp.status_code}): {_error_text(resp)}",
                            resp.status_code)
    return resp


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_pkce_pair() -> Tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))  # 86 chars, within RFC 7636's 43..128
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _email_from_id_token(id_token: Optional[str]) -> Optional[str]:
    """Read ``email`` from the id_token payload.

    The token arrives directly from Google's token endpoint over TLS in exchange
    for our own code, so its signature need not be verified to display the
    account name; it is never used for authorisation decisions.
    """
    if not id_token or id_token.count(".") != 2:
        return None
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email")
    except Exception:
        return None


def _page(title: str, message: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Volt AI - {html.escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;background:#0b1220;color:#e6f7ff;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
        "main{max-width:32rem;padding:2rem;text-align:center}h1{color:#22d3ee}</style>"
        f"</head><body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        "</main></body></html>"
    ).encode("utf-8")


class _LoopbackServer(HTTPServer):
    """One-shot loopback receiver for the OAuth redirect."""

    allow_reuse_address = False

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.timeout = 0.5
        self.params: Optional[Dict[str, str]] = None
        self.got_params = threading.Event()
        self.outcome_ready = threading.Event()
        self.outcome: Tuple[int, bytes] = (500, _page("Sign-in failed", "Please try again."))


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _LoopbackServer

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/" or self.server.got_params.is_set():
            self.send_response(404)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        self.server.params = {k: v[0] for k, v in query.items()}
        self.server.got_params.set()
        # Wait for the main thread to validate and exchange the code, so the
        # browser page tells the truth about whether sign-in worked.
        self.server.outcome_ready.wait(60)
        status, body = self.server.outcome
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # keep codes out of logs
        pass


def _exchange_code(http: Any, client: Dict[str, str], code: str, verifier: str,
                   redirect_uri: str) -> Dict[str, Any]:
    resp = http.request("POST", client["token_uri"], data={
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
    }, timeout=API_TIMEOUT)
    if not (200 <= resp.status_code < 300):
        raise OAuthError(f"Google refused the sign-in code (HTTP {resp.status_code}): "
                         f"{_error_text(resp)}")
    return resp.json()


def sign_in(open_browser: Callable[[str], Any] = webbrowser.open, timeout: float = 300,
            *, session: Optional[Any] = None) -> str:
    """Sign in with Google in the system browser and store tokens; return the email.

    Raises ``MissingOAuthClientError`` if the client file is absent,
    ``OAuthStateError`` on a forged callback, or ``OAuthError`` on denial,
    timeout or a failed code exchange.
    """
    client = load_client_config()
    http = _http(session)
    verifier, challenge = _make_pkce_pair()
    state = secrets.token_urlsafe(32)

    server = _LoopbackServer()
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "scope": SCOPE_STRING,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",  # ensures Google issues a refresh token every time
    })

    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            server.handle_request()

    worker = threading.Thread(target=serve, name="volt-oauth-loopback", daemon=True)
    worker.start()
    debug_log(f"OAuth loopback listening on {redirect_uri}", _LOG)
    try:
        print("🔐 Opening your browser to sign in to Google for Volt AI backups…", flush=True)
        try:
            opened = open_browser(auth_url)
        except Exception as exc:
            debug_log(f"could not open browser: {exc}", _LOG)
            opened = False
        if opened is False:
            print(f"🌐 Open this link to sign in: {auth_url}", flush=True)

        if not server.got_params.wait(timeout):
            raise OAuthError(f"Google sign-in timed out after {int(timeout)} seconds.")
        params = server.params or {}

        try:
            if not secrets.compare_digest(params.get("state", ""), state):
                debug_log("OAuth callback rejected: state mismatch", _LOG)
                raise OAuthStateError(
                    "Sign-in response did not match this request (state mismatch); "
                    "nothing was saved. Please try again.")
            if "error" in params:
                raise OAuthError(f"Google sign-in was not completed: {params['error']}")
            code = params.get("code")
            if not code:
                raise OAuthError("Google did not return an authorisation code.")

            tokens = _exchange_code(http, client, code, verifier, redirect_uri)
            if not tokens.get("refresh_token") or not tokens.get("access_token"):
                raise OAuthError("Google did not return the expected tokens.")
            email = _email_from_id_token(tokens.get("id_token"))
            if not email:
                email = _fetch_userinfo_email(http, tokens["access_token"])
            granted = set((tokens.get("scope") or SCOPE_STRING).split())
            if DRIVE_FILE_SCOPE not in granted:
                raise OAuthError("Google Drive access was not granted, so backups "
                                 "cannot work. Please sign in again and allow it.")
            _write_private_json(token_file_path(), {
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "expiry": time.time() + float(tokens.get("expires_in", 3600)),
                "email": email,
                "scope": tokens.get("scope", SCOPE_STRING),
            })
        except OAuthError as exc:
            server.outcome = (400, _page("Sign-in failed",
                                         f"{exc} You can close this tab and go back to Volt AI."))
            raise
        except Exception:
            server.outcome = (500, _page("Sign-in failed",
                                         "Something went wrong. You can close this tab and "
                                         "go back to Volt AI to try again."))
            raise
        else:
            server.outcome = (200, _page(
                "Signed in to Volt AI",
                f"Signed in as {email or 'your Google account'}. "
                "You can close this tab and go back to Volt AI."))
        finally:
            server.outcome_ready.set()

        debug_log(f"signed in to Google as {email}", _LOG)
        print(f"✅ Signed in to Google as {email}", flush=True)
        return email or ""
    finally:
        server.outcome_ready.set()
        stop.set()
        worker.join(5)
        server.server_close()


def _fetch_userinfo_email(http: Any, access_token: str) -> Optional[str]:
    try:
        resp = http.request("GET", USERINFO_URL,
                            headers={"Authorization": f"Bearer {access_token}"},
                            timeout=API_TIMEOUT)
        if 200 <= resp.status_code < 300:
            return resp.json().get("email")
    except Exception as exc:
        debug_log(f"userinfo lookup failed: {exc}", _LOG)
    return None


def signed_in_email() -> Optional[str]:
    """Return the signed-in Google account's email, or ``None`` if signed out."""
    tokens = _load_tokens()
    return (tokens or {}).get("email") or None


def sign_out(*, session: Optional[Any] = None) -> None:
    """Revoke the stored Google token (best effort) and delete the token file."""
    tokens = _load_tokens()
    path = token_file_path()
    if tokens:
        try:
            resp = _http(session).request(
                "POST", REVOKE_URL, data={"token": tokens["refresh_token"]},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=API_TIMEOUT)
            debug_log(f"token revoke returned HTTP {resp.status_code}", _LOG)
        except Exception as exc:
            debug_log(f"token revoke failed (ignored): {exc}", _LOG)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    print("👋 Signed out of Google; Volt AI backups are disconnected.", flush=True)


# ---------------------------------------------------------------------------
# Authorised Drive client
# ---------------------------------------------------------------------------


class _Drive:
    def __init__(self, session: Optional[Any]) -> None:
        self.http = _http(session)
        tokens = _load_tokens()
        if not tokens:
            raise NotSignedInError("Not signed in to Google. Sign in to use Drive backups.")
        self.tokens = tokens

    def _refresh(self) -> None:
        client = load_client_config()
        debug_log("refreshing Google access token", _LOG)
        resp = self.http.request("POST", client["token_uri"], data={
            "grant_type": "refresh_token",
            "refresh_token": self.tokens["refresh_token"],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
        }, timeout=API_TIMEOUT)
        if resp.status_code in (400, 401):
            raise NotSignedInError("Your Google sign-in has expired or was revoked. "
                                   "Please sign in again.")
        _check(resp, "Refreshing the Google sign-in")
        body = resp.json()
        self.tokens["access_token"] = body["access_token"]
        self.tokens["expiry"] = time.time() + float(body.get("expires_in", 3600))
        if body.get("refresh_token"):
            self.tokens["refresh_token"] = body["refresh_token"]
        _write_private_json(token_file_path(), self.tokens)

    def _access_token(self) -> str:
        expiry = float(self.tokens.get("expiry") or 0)
        if not self.tokens.get("access_token") or expiry - EXPIRY_MARGIN_SECONDS <= time.time():
            self._refresh()
        return self.tokens["access_token"]

    def request(self, method: str, url: str, *, retry_auth: bool = True,
                headers: Optional[Dict[str, str]] = None, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", API_TIMEOUT)
        hdrs = dict(headers or {})
        hdrs["Authorization"] = f"Bearer {self._access_token()}"
        resp = self.http.request(method, url, headers=hdrs, **kwargs)
        if resp.status_code == 401 and retry_auth:
            debug_log("Drive rejected access token; refreshing once", _LOG)
            self._refresh()
            hdrs["Authorization"] = f"Bearer {self.tokens['access_token']}"
            resp = self.http.request(method, url, headers=hdrs, **kwargs)
        return resp

    # --- Drive operations -------------------------------------------------
    @staticmethod
    def _q_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def _list(self, q: str) -> List[Dict[str, Any]]:
        resp = self.request("GET", FILES_URL, params={
            "q": q, "spaces": "drive", "orderBy": "modifiedTime desc",
            "fields": f"files({FILE_FIELDS},parents)", "pageSize": "10",
        })
        return _check(resp, "Searching Google Drive").json().get("files", [])

    def find_folder(self) -> Optional[str]:
        files = self._list(f"name = '{self._q_escape(BACKUP_FOLDER_NAME)}' and "
                           f"mimeType = '{FOLDER_MIME}' and trashed = false")
        return files[0]["id"] if files else None

    def ensure_folder(self) -> str:
        folder_id = self.find_folder()
        if folder_id:
            return folder_id
        debug_log(f"creating Drive folder {BACKUP_FOLDER_NAME!r}", _LOG)
        resp = self.request("POST", FILES_URL, params={"fields": "id"},
                            json={"name": BACKUP_FOLDER_NAME, "mimeType": FOLDER_MIME})
        return _check(resp, "Creating the Volt AI Backup folder").json()["id"]

    def find_backup(self, folder_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not folder_id:
            return None
        files = self._list(f"name = '{self._q_escape(BACKUP_FILE_NAME)}' and "
                           f"'{self._q_escape(folder_id)}' in parents and trashed = false")
        return files[0] if files else None

    def upload(self, zip_path: Path, folder_id: str, existing_id: Optional[str]) -> Dict[str, Any]:
        size = zip_path.stat().st_size
        url = f"{UPLOAD_URL}/{existing_id}" if existing_id else UPLOAD_URL
        method = "PATCH" if existing_id else "POST"
        meta: Dict[str, Any] = {"name": BACKUP_FILE_NAME, "mimeType": ZIP_MIME}
        if not existing_id:
            meta["parents"] = [folder_id]

        if size <= RESUMABLE_THRESHOLD_BYTES:
            boundary = f"volt{uuid.uuid4().hex}"
            body = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(meta)}\r\n--{boundary}\r\nContent-Type: {ZIP_MIME}\r\n\r\n"
            ).encode("utf-8") + zip_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
            resp = self.request(
                method, url, params={"uploadType": "multipart", "fields": FILE_FIELDS},
                headers={"Content-Type": f"multipart/related; boundary={boundary}"},
                data=body, timeout=TRANSFER_TIMEOUT)
            return _check(resp, "Uploading the backup").json()

        debug_log(f"using resumable upload for {size} bytes", _LOG)
        start = self.request(
            method, url, params={"uploadType": "resumable", "fields": FILE_FIELDS},
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": ZIP_MIME,
                     "X-Upload-Content-Length": str(size)},
            json=meta)
        _check(start, "Starting the backup upload")
        location = start.headers.get("Location") or start.headers.get("location")
        if not location:
            raise DriveApiError("Google Drive did not return an upload session URL.")
        with zip_path.open("rb") as fh:
            # A streamed body cannot be replayed, so no automatic 401 retry here.
            resp = self.request("PUT", location, retry_auth=False, data=fh,
                                headers={"Content-Type": ZIP_MIME,
                                         "Content-Length": str(size)},
                                timeout=TRANSFER_TIMEOUT)
        return _check(resp, "Uploading the backup").json()

    def download(self, file_id: str, dest: Path) -> None:
        resp = self.request("GET", f"{FILES_URL}/{file_id}", params={"alt": "media"},
                            stream=True, timeout=TRANSFER_TIMEOUT)
        _check(resp, "Downloading the backup")
        try:
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()


def _summarise(meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": meta.get("id"),
        "name": meta.get("name", BACKUP_FILE_NAME),
        "size": int(meta["size"]) if meta.get("size") is not None else None,
        "modifiedTime": meta.get("modifiedTime"),
        "md5Checksum": meta.get("md5Checksum"),
    }


# ---------------------------------------------------------------------------
# Archive building and safe extraction
# ---------------------------------------------------------------------------


def _should_skip_name(name: str) -> bool:
    return name.endswith(SKIP_SUFFIXES)


def _sqlite_snapshot(src: Path, dest: Path) -> bool:
    """Copy a live SQLite database consistently with the backup API."""
    try:
        source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        debug_log(f"could not open {src.name} as SQLite ({exc}); copying raw", _LOG)
        return False
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
        return True
    except sqlite3.Error as exc:
        debug_log(f"SQLite backup of {src.name} failed ({exc}); copying raw", _LOG)
        return False
    finally:
        source.close()


def _build_archive(data_dir: Path, zip_path: Path) -> Tuple[int, List[str]]:
    files = 0
    skipped: List[str] = []
    with tempfile.TemporaryDirectory(prefix="volt-ai-sqlite-") as snap_dir, \
            zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, names in os.walk(data_dir, followlinks=False):
            dirs.sort()
            for name in sorted(names):
                path = Path(root) / name
                arcname = path.relative_to(data_dir).as_posix()
                if _should_skip_name(name):
                    continue
                try:
                    st = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):  # skip symlinks, sockets, etc.
                    continue
                # Databases hold the chats themselves, so they are always kept.
                if st.st_size > MAX_FILE_BYTES and not name.endswith(".db"):
                    debug_log(f"skipping {arcname}: {st.st_size} bytes exceeds limit", _LOG)
                    skipped.append(arcname)
                    continue
                if name.endswith(".db"):
                    snap = Path(snap_dir) / f"{files}.db"
                    if _sqlite_snapshot(path, snap):
                        zf.write(snap, arcname)
                        snap.unlink()
                        files += 1
                        continue
                zf.write(path, arcname)
                files += 1
    return files, skipped


_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


def _safe_member_parts(name: str) -> Tuple[str, ...]:
    """Validate a zip entry name and return its path parts, rejecting zip-slip."""
    if not name or "\x00" in name:
        raise UnsafeBackupError(f"Backup contains an invalid entry name: {name!r}")
    if name.startswith(("/", "\\")) or _DRIVE_LETTER.match(name):
        raise UnsafeBackupError(f"Backup contains an absolute path: {name!r}")
    parts = tuple(p for p in re.split(r"[\\/]+", name) if p not in ("", "."))
    if any(p == ".." for p in parts):
        raise UnsafeBackupError(f"Backup contains a path that escapes the folder: {name!r}")
    return parts


def _validate_archive(zip_path: Path) -> List[Tuple[zipfile.ZipInfo, Tuple[str, ...]]]:
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise UnsafeBackupError(f"The downloaded backup is not a valid zip file: {exc}") from exc
    with zf:
        members = [(info, _safe_member_parts(info.filename)) for info in zf.infolist()]
        bad = zf.testzip()
        if bad is not None:
            raise UnsafeBackupError(f"The downloaded backup is corrupt (bad entry {bad!r}).")
    return members


def _extract(zip_path: Path, members: List[Tuple[zipfile.ZipInfo, Tuple[str, ...]]],
             dest: Path) -> int:
    root = dest.resolve()
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info, parts in members:
            if not parts:
                continue
            target = root.joinpath(*parts)
            if target != root and root not in target.resolve().parents:
                raise UnsafeBackupError(f"Backup entry escapes the folder: {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            count += 1
    return count


def _aside_path(data_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = data_dir.with_name(f"{data_dir.name}.before-restore-{stamp}")
    n = 1
    while candidate.exists():
        candidate = data_dir.with_name(f"{data_dir.name}.before-restore-{stamp}-{n}")
        n += 1
    return candidate


# ---------------------------------------------------------------------------
# Public backup API
# ---------------------------------------------------------------------------


def backup_now(data_dir: Path, *, session: Optional[Any] = None) -> Dict[str, Any]:
    """Zip ``data_dir`` and upload it to ``Volt AI Backup/volt-ai-backup.zip`` in Drive.

    Returns the Drive file's ``id``, ``name``, ``size`` (bytes), ``modifiedTime``
    and ``md5Checksum``, plus ``files`` (entries archived) and ``skipped``
    (files left out for exceeding ``MAX_FILE_BYTES``).
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise VoltDriveError(f"Volt AI data folder not found: {data_dir}")
    drive = _Drive(session)
    fd, tmp_name = tempfile.mkstemp(prefix="volt-ai-backup-", suffix=".zip")
    os.close(fd)
    zip_path = Path(tmp_name)
    try:
        print("📦 Packing Volt AI data for backup…", flush=True)
        count, skipped = _build_archive(data_dir, zip_path)
        debug_log(f"archive built: {count} files, {zip_path.stat().st_size} bytes, "
                  f"{len(skipped)} skipped", _LOG)
        folder_id = drive.ensure_folder()
        existing = drive.find_backup(folder_id)
        print("☁️ Uploading backup to your Google Drive…", flush=True)
        meta = drive.upload(zip_path, folder_id, existing["id"] if existing else None)
    finally:
        try:
            zip_path.unlink()
        except OSError:
            pass
    result = _summarise(meta)
    result.update({"files": count, "skipped": skipped})
    debug_log(f"backup uploaded: id={result['id']} size={result['size']}", _LOG)
    size_mb = (result["size"] or 0) / (1024 * 1024)
    print(f"✅ Backed up {count} files ({size_mb:.1f} MB) to Google Drive.", flush=True)
    if skipped:
        print(f"⚠️ Skipped {len(skipped)} file(s) larger than "
              f"{MAX_FILE_BYTES // (1024 * 1024)} MB.", flush=True)
    return result


def last_backup_info(*, session: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """Return metadata of the Drive backup (``modifiedTime``, ``size``…), or ``None``."""
    drive = _Drive(session)
    existing = drive.find_backup(drive.find_folder())
    return _summarise(existing) if existing else None


def restore_latest(data_dir: Path, *, session: Optional[Any] = None) -> Dict[str, Any]:
    """Download the Drive backup and extract it into ``data_dir``.

    The archive is downloaded and validated first; only then is any existing
    ``data_dir`` moved aside to ``<name>.before-restore-<timestamp>`` next to
    it, so nothing is ever deleted. If extraction fails the original folder is
    put back. Returns the backup metadata plus ``restored_to``,
    ``previous_data_dir`` (or ``None``) and ``files`` (entries extracted).
    """
    data_dir = Path(data_dir)
    drive = _Drive(session)
    existing = drive.find_backup(drive.find_folder())
    if not existing:
        raise NoBackupError("No Volt AI backup was found in your Google Drive.")

    fd, tmp_name = tempfile.mkstemp(prefix="volt-ai-restore-", suffix=".zip")
    os.close(fd)
    zip_path = Path(tmp_name)
    try:
        print("☁️ Downloading your Volt AI backup from Google Drive…", flush=True)
        drive.download(existing["id"], zip_path)
        members = _validate_archive(zip_path)
        debug_log(f"backup downloaded and validated: {len(members)} entries", _LOG)

        aside: Optional[Path] = None
        if data_dir.exists() or data_dir.is_symlink():
            aside = _aside_path(data_dir)
            data_dir.rename(aside)
            debug_log(f"moved current data aside to {aside}", _LOG)
        data_dir.mkdir(parents=True, exist_ok=False)
        try:
            count = _extract(zip_path, members, data_dir)
        except BaseException:
            shutil.rmtree(data_dir, ignore_errors=True)
            if aside is not None:
                aside.rename(data_dir)
            raise
    finally:
        try:
            zip_path.unlink()
        except OSError:
            pass

    result = _summarise(existing)
    result.update({"restored_to": str(data_dir),
                   "previous_data_dir": str(aside) if aside else None,
                   "files": count})
    print(f"✅ Restored {count} files from your backup.", flush=True)
    if aside:
        print(f"🗂️ Your previous data was kept at {aside}", flush=True)
    return result


__all__ = [
    "BACKUP_FILE_NAME", "BACKUP_FOLDER_NAME", "DRIVE_FILE_SCOPE", "SCOPES",
    "VoltDriveError", "MissingOAuthClientError", "OAuthError", "OAuthStateError",
    "NotSignedInError", "DriveApiError", "NoBackupError", "UnsafeBackupError",
    "sign_in", "sign_out", "signed_in_email", "backup_now", "restore_latest",
    "last_backup_info", "client_file_path", "token_file_path",
]
