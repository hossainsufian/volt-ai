"""Behavioural tests for the Volt AI Google Drive backup module.

Every test runs against an in-memory fake of Google's OAuth and Drive
endpoints; no real network traffic is made and HOME is pointed at tmp_path
so the real home directory is never touched.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sqlite3
import stat
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pytest

from desktop_app import volt_drive_backup as vdb


# ---------------------------------------------------------------------------
# Fake Google
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", headers=None):
        self.status_code = status_code
        self._json = json_data
        if json_data is not None and not content:
            content = json.dumps(json_data).encode()
        self.content = content
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        if self._json is None:
            return json.loads(self.content or b"null")
        return self._json

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def close(self):
        pass


def _fake_id_token(email: str) -> str:
    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{seg({'alg': 'RS256'})}.{seg({'email': email, 'email_verified': True})}.sig"


class FakeGoogle:
    """Minimal emulation of the Google OAuth token endpoint and Drive v3."""

    def __init__(self):
        self.calls = []
        self.files = {}  # id -> dict(meta..., content=bytes)
        self.valid_tokens = {"access-1"}
        self.token_requests = []
        self.revoked = []
        self._next_id = 0
        self._next_token = 1
        self._resumable = {}

    # --- helpers -----------------------------------------------------------
    def _new_id(self):
        self._next_id += 1
        return f"id{self._next_id}"

    def _meta(self, f):
        return {
            "id": f["id"],
            "name": f["name"],
            "mimeType": f["mimeType"],
            "parents": f.get("parents", []),
            "size": str(len(f.get("content", b""))),
            "modifiedTime": f["modifiedTime"],
        }

    def _stamp(self):
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()) + f"#{len(self.calls)}"

    def _authorised(self, headers):
        auth = (headers or {}).get("Authorization", "")
        return auth.startswith("Bearer ") and auth[7:] in self.valid_tokens

    def put_file(self, name, content, parents, mime="application/zip"):
        fid = self._new_id()
        self.files[fid] = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": parents,
            "content": content,
            "modifiedTime": self._stamp(),
        }
        return fid

    def folders(self):
        return [f for f in self.files.values() if f["mimeType"] == vdb.FOLDER_MIME]

    def backups(self):
        return [f for f in self.files.values() if f["name"] == vdb.BACKUP_FILE_NAME]

    @staticmethod
    def _parse_multipart(headers, body):
        ctype = headers["Content-Type"]
        assert ctype.startswith("multipart/related")
        boundary = re.search(r"boundary=([^;]+)", ctype).group(1).strip('"').encode()
        parts = body.split(b"--" + boundary)[1:-1]
        out = []
        for part in parts:
            part = part[2:] if part.startswith(b"\r\n") else part
            head, _, payload = part.partition(b"\r\n\r\n")
            out.append(payload[:-2] if payload.endswith(b"\r\n") else payload)
        return json.loads(out[0]), out[1]

    @staticmethod
    def _read(data):
        if data is None:
            return b""
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        if hasattr(data, "read"):
            return data.read()
        return b"".join(data)

    # --- requests.Session interface ---------------------------------------
    def request(self, method, url, params=None, data=None, json=None, headers=None,
                stream=False, timeout=None, **_kw):
        method = method.upper()
        self.calls.append({"method": method, "url": url, "params": dict(params or {}),
                           "data": data, "json": json, "headers": dict(headers or {})})
        params = params or {}

        if url == vdb.TOKEN_URL:
            self.token_requests.append(dict(data))
            self._next_token += 1
            tok = f"access-{self._next_token}"
            self.valid_tokens.add(tok)
            body = {"access_token": tok, "expires_in": 3599, "token_type": "Bearer",
                    "scope": vdb.SCOPE_STRING}
            if data["grant_type"] == "authorization_code":
                body["refresh_token"] = "refresh-abc"
                body["id_token"] = _fake_id_token("sufian@example.com")
            return FakeResponse(200, body)

        if url == vdb.REVOKE_URL:
            self.revoked.append(dict(data or {}))
            return FakeResponse(200, {})

        if url.startswith("https://upload.fake/session/"):
            pending = self._resumable.pop(url)
            content = self._read(data)
            return self._store(pending["file_id"], pending["meta"], content)

        if not self._authorised(headers):
            return FakeResponse(401, {"error": {"code": 401, "message": "Invalid Credentials"}})

        if url == vdb.FILES_URL and method == "GET":
            return FakeResponse(200, {"files": [self._meta(f) for f in self._query(params["q"])]})

        if url == vdb.FILES_URL and method == "POST":
            fid = self.put_file(json["name"], b"", json.get("parents", []), json["mimeType"])
            return FakeResponse(200, self._meta(self.files[fid]))

        if url.startswith(vdb.FILES_URL + "/") and method == "GET":
            fid = url.rsplit("/", 1)[1]
            f = self.files.get(fid)
            if f is None:
                return FakeResponse(404, {"error": {"code": 404, "message": "not found"}})
            if params.get("alt") == "media":
                return FakeResponse(200, content=f["content"])
            return FakeResponse(200, self._meta(f))

        if url.startswith(vdb.UPLOAD_URL):
            file_id = url[len(vdb.UPLOAD_URL):].lstrip("/") or None
            assert method == ("PATCH" if file_id else "POST")
            if params["uploadType"] == "multipart":
                meta, content = self._parse_multipart(headers, self._read(data))
                return self._store(file_id, meta, content)
            if params["uploadType"] == "resumable":
                loc = f"https://upload.fake/session/{len(self._resumable) + len(self.calls)}"
                self._resumable[loc] = {"file_id": file_id, "meta": json or {}}
                return FakeResponse(200, content=b"", headers={"Location": loc})

        raise AssertionError(f"unexpected request {method} {url} {params}")

    def _store(self, file_id, meta, content):
        if file_id is None:
            file_id = self.put_file(meta["name"], content, meta.get("parents", []),
                                    meta.get("mimeType", "application/octet-stream"))
        else:
            f = self.files[file_id]
            f["content"] = content
            f["modifiedTime"] = self._stamp()
        return FakeResponse(200, self._meta(self.files[file_id]))

    def _query(self, q):
        name = re.search(r"name\s*=\s*'((?:[^'\\]|\\.)*)'", q)
        parent = re.search(r"'([^']+)'\s+in\s+parents", q)
        folder = "mimeType = 'application/vnd.google-apps.folder'" in q
        out = []
        for f in self.files.values():
            if name and f["name"] != name.group(1).replace("\\'", "'"):
                continue
            if parent and parent.group(1) not in f.get("parents", []):
                continue
            if folder and f["mimeType"] != vdb.FOLDER_MIME:
                continue
            out.append(f)
        return out

    # convenience
    def methods_to(self, prefix):
        return [(c["method"], c["params"].get("uploadType")) for c in self.calls
                if c["url"].startswith(prefix)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert Path.home() == home
    return home


@pytest.fixture
def config_dir(home):
    return home / ".config" / "volt-ai"


@pytest.fixture
def client_file(config_dir):
    config_dir.mkdir(parents=True)
    path = config_dir / "google_oauth_client.json"
    path.write_text(json.dumps({"installed": {
        "client_id": "123-abc.apps.googleusercontent.com",
        "client_secret": "GOCSPX-test",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}))
    return path


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def signed_in(config_dir, client_file):
    """A token file with a still-valid access token."""
    (config_dir / "google_token.json").write_text(json.dumps({
        "access_token": "access-1",
        "refresh_token": "refresh-abc",
        "expiry": time.time() + 3000,
        "email": "sufian@example.com",
    }))
    return config_dir / "google_token.json"


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "volt-data"
    (d / "images").mkdir(parents=True)
    (d / "chats.json").write_text('{"chats": ["hello"]}')
    (d / "images" / "cat.png").write_bytes(b"\x89PNG fake image bytes")
    return d


def _browser_that_returns(state_override=None, captured=None):
    """Fake browser: follows the consent URL straight to the loopback redirect."""

    def open_browser(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if captured is not None:
            captured["url"] = url
            captured["query"] = {k: v[0] for k, v in q.items()}
        state = state_override if state_override is not None else q["state"][0]
        target = q["redirect_uri"][0] + "?" + urllib.parse.urlencode(
            {"code": "auth-code-xyz", "state": state})

        def hit():
            try:
                with urllib.request.urlopen(target, timeout=10) as resp:
                    body = resp.read().decode()
            except urllib.error.HTTPError as exc:
                body = exc.read().decode()
            if captured is not None:
                captured["page"] = body

        t = threading.Thread(target=hit, daemon=True)
        t.start()
        if captured is not None:
            captured["thread"] = t
        return True

    return open_browser


def _zip_names(content: bytes):
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return set(zf.namelist())


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def test_missing_client_file_gives_clear_error(home, google):
    with pytest.raises(vdb.MissingOAuthClientError) as excinfo:
        vdb.sign_in(open_browser=lambda url: True, timeout=1, session=google)
    msg = str(excinfo.value)
    assert str(home / ".config" / "volt-ai" / "google_oauth_client.json") in msg
    assert "Desktop app" in msg
    assert "Drive API" in msg
    assert google.calls == []


def test_client_file_without_installed_section_is_rejected(client_file, google):
    client_file.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
    with pytest.raises(vdb.MissingOAuthClientError, match="Desktop app"):
        vdb.sign_in(open_browser=lambda url: True, timeout=1, session=google)


def test_sign_in_uses_pkce_and_writes_private_token_file(client_file, config_dir, google):
    captured = {}
    email = vdb.sign_in(open_browser=_browser_that_returns(captured=captured),
                        timeout=20, session=google)
    captured["thread"].join(5)

    assert email == "sufian@example.com"
    q = captured["query"]
    assert q["scope"].split() == ["openid", "email", "https://www.googleapis.com/auth/drive.file"]
    assert q["code_challenge_method"] == "S256"
    assert q["response_type"] == "code"
    assert q["access_type"] == "offline"
    assert urllib.parse.urlparse(q["redirect_uri"]).hostname == "127.0.0.1"
    assert urllib.parse.urlparse(q["redirect_uri"]).port

    # The verifier sent at exchange time must hash to the challenge in the URL.
    (exchange,) = google.token_requests
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "auth-code-xyz"
    assert exchange["redirect_uri"] == q["redirect_uri"]
    digest = hashlib.sha256(exchange["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"]
    assert 43 <= len(exchange["code_verifier"]) <= 128

    assert "close this tab" in captured["page"]
    assert "Volt AI" in captured["page"]

    token_path = config_dir / "google_token.json"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    stored = json.loads(token_path.read_text())
    assert stored["refresh_token"] == "refresh-abc"
    assert stored["email"] == "sufian@example.com"
    assert stored["expiry"] > time.time()
    assert vdb.signed_in_email() == "sufian@example.com"


def test_sign_in_rejects_mismatched_state(client_file, config_dir, google):
    captured = {}
    with pytest.raises(vdb.OAuthStateError):
        vdb.sign_in(open_browser=_browser_that_returns(state_override="forged", captured=captured),
                    timeout=20, session=google)
    captured["thread"].join(5)
    assert google.token_requests == []
    assert not (config_dir / "google_token.json").exists()
    assert vdb.signed_in_email() is None
    assert "Volt AI" in captured["page"]


def test_sign_in_times_out_without_browser_callback(client_file, google):
    with pytest.raises(vdb.OAuthError, match="timed out"):
        vdb.sign_in(open_browser=lambda url: True, timeout=1, session=google)


def test_expired_access_token_is_refreshed(signed_in, google):
    data = json.loads(signed_in.read_text())
    data["expiry"] = time.time() - 10
    data["access_token"] = "stale"
    signed_in.write_text(json.dumps(data))

    assert vdb.last_backup_info(session=google) is None

    (refresh,) = google.token_requests
    assert refresh["grant_type"] == "refresh_token"
    assert refresh["refresh_token"] == "refresh-abc"
    drive_calls = [c for c in google.calls if c["url"].startswith(vdb.FILES_URL)]
    assert drive_calls and all(c["headers"]["Authorization"] == "Bearer access-2"
                               for c in drive_calls)
    stored = json.loads(signed_in.read_text())
    assert stored["access_token"] == "access-2"
    assert stored["refresh_token"] == "refresh-abc"  # kept when Google omits it
    assert stored["expiry"] > time.time()
    assert stat.S_IMODE(signed_in.stat().st_mode) == 0o600


def test_rejected_access_token_triggers_one_refresh_and_retry(signed_in, google):
    google.valid_tokens.clear()  # access-1 revoked server side, refresh still works
    assert vdb.last_backup_info(session=google) is None
    assert len(google.token_requests) == 1


def test_not_signed_in_raises(client_file, google, data_dir):
    with pytest.raises(vdb.NotSignedInError):
        vdb.backup_now(data_dir, session=google)


def test_sign_out_revokes_and_deletes_token(signed_in, google):
    vdb.sign_out(session=google)
    assert google.revoked and google.revoked[0]["token"] == "refresh-abc"
    assert not signed_in.exists()
    assert vdb.signed_in_email() is None


def test_sign_out_survives_revoke_failure(signed_in):
    class Broken:
        def request(self, *a, **k):
            raise OSError("offline")

    vdb.sign_out(session=Broken())
    assert not signed_in.exists()


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def test_backup_creates_folder_once_and_updates_file_in_place(signed_in, google, data_dir):
    first = vdb.backup_now(data_dir, session=google)
    (data_dir / "chats.json").write_text('{"chats": ["hello", "second"]}')
    second = vdb.backup_now(data_dir, session=google)

    assert len(google.folders()) == 1
    assert google.folders()[0]["name"] == "Volt AI Backup"
    backups = google.backups()
    assert len(backups) == 1
    assert backups[0]["parents"] == [google.folders()[0]["id"]]
    assert first["id"] == second["id"] == backups[0]["id"]
    assert second["size"] == len(backups[0]["content"])
    assert second["modifiedTime"] == backups[0]["modifiedTime"]

    uploads = google.methods_to(vdb.UPLOAD_URL)
    assert uploads == [("POST", "multipart"), ("PATCH", "multipart")]
    folder_creates = [c for c in google.calls
                      if c["url"] == vdb.FILES_URL and c["method"] == "POST"]
    assert len(folder_creates) == 1

    with zipfile.ZipFile(io.BytesIO(backups[0]["content"])) as zf:
        assert json.loads(zf.read("chats.json")) == {"chats": ["hello", "second"]}
        assert zf.read("images/cat.png") == b"\x89PNG fake image bytes"


def test_backup_captures_live_sqlite_consistently_and_skips_sidecars(
        signed_in, google, data_dir, monkeypatch):
    db = data_dir / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE notes (body TEXT)")
    conn.executemany("INSERT INTO notes VALUES (?)", [(f"note {i}",) for i in range(50)])
    conn.commit()
    # Rows live in the WAL; a raw copy of memory.db alone would miss them.
    assert (data_dir / "memory.db-wal").exists()
    (data_dir / "app.lock").write_text("pid")
    (data_dir / "huge.bin").write_bytes(b"x" * 2048)
    monkeypatch.setattr(vdb, "MAX_FILE_BYTES", 1024)

    try:
        result = vdb.backup_now(data_dir, session=google)
    finally:
        conn.close()

    content = google.backups()[0]["content"]
    names = _zip_names(content)
    assert "memory.db" in names
    assert not any(n.endswith(("-wal", "-shm", ".lock")) for n in names)
    assert "huge.bin" not in names
    assert "huge.bin" in result["skipped"]

    out = Path(signed_in.parent) / "restored.db"
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        out.write_bytes(zf.read("memory.db"))
    check = sqlite3.connect(out)
    try:
        assert check.execute("SELECT count(*) FROM notes").fetchone()[0] == 50
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


def test_large_backup_uses_resumable_upload(signed_in, google, data_dir, monkeypatch):
    monkeypatch.setattr(vdb, "RESUMABLE_THRESHOLD_BYTES", 0)
    vdb.backup_now(data_dir, session=google)
    vdb.backup_now(data_dir, session=google)
    assert google.methods_to(vdb.UPLOAD_URL) == [("POST", "resumable"), ("PATCH", "resumable")]
    assert len(google.backups()) == 1
    assert "chats.json" in _zip_names(google.backups()[0]["content"])


def test_backup_rejects_missing_data_dir(signed_in, google, tmp_path):
    with pytest.raises(vdb.VoltDriveError):
        vdb.backup_now(tmp_path / "nope", session=google)


def test_last_backup_info_reports_drive_file(signed_in, google, data_dir):
    assert vdb.last_backup_info(session=google) is None
    result = vdb.backup_now(data_dir, session=google)
    info = vdb.last_backup_info(session=google)
    assert info["id"] == result["id"]
    assert info["size"] == result["size"]
    assert info["modifiedTime"] == result["modifiedTime"]


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def test_restore_moves_current_dir_aside_and_extracts(signed_in, google, data_dir):
    vdb.backup_now(data_dir, session=google)
    (data_dir / "chats.json").write_text('{"chats": ["changed after backup"]}')
    (data_dir / "new-only.txt").write_text("made after backup")

    result = vdb.restore_latest(data_dir, session=google)

    aside = Path(result["previous_data_dir"])
    assert aside.parent == data_dir.parent
    assert aside.name.startswith("volt-data.before-restore-")
    assert json.loads((aside / "chats.json").read_text()) == {"chats": ["changed after backup"]}
    assert (aside / "new-only.txt").exists()

    assert json.loads((data_dir / "chats.json").read_text()) == {"chats": ["hello"]}
    assert (data_dir / "images" / "cat.png").read_bytes() == b"\x89PNG fake image bytes"
    assert not (data_dir / "new-only.txt").exists()
    assert result["files"] == 2


def test_restore_into_missing_dir_does_not_need_aside(signed_in, google, data_dir, tmp_path):
    vdb.backup_now(data_dir, session=google)
    fresh = tmp_path / "fresh-install" / "volt-data"
    result = vdb.restore_latest(fresh, session=google)
    assert result["previous_data_dir"] is None
    assert (fresh / "chats.json").exists()


@pytest.mark.parametrize("evil", ["../escape.txt", "/etc/evil.txt", "a/../../escape.txt",
                                  "..\\escape.txt", "C:/evil.txt"])
def test_restore_rejects_zip_slip(signed_in, google, data_dir, tmp_path, evil):
    folder = google.put_file("Volt AI Backup", b"", [], vdb.FOLDER_MIME)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("chats.json", "{}")
        zf.writestr(evil, "pwned")
    google.put_file(vdb.BACKUP_FILE_NAME, buf.getvalue(), [folder])
    before = sorted(p.name for p in tmp_path.iterdir())

    with pytest.raises(vdb.UnsafeBackupError):
        vdb.restore_latest(data_dir, session=google)

    assert sorted(p.name for p in tmp_path.iterdir()) == before  # nothing moved aside
    assert json.loads((data_dir / "chats.json").read_text()) == {"chats": ["hello"]}
    assert not (tmp_path / "escape.txt").exists()


def test_restore_without_backup_raises(signed_in, google, data_dir):
    with pytest.raises(vdb.NoBackupError):
        vdb.restore_latest(data_dir, session=google)
    assert (data_dir / "chats.json").exists()


def test_core_package_does_not_import_module():
    root = Path(__file__).resolve().parents[1] / "src" / "jarvis"
    for py in root.rglob("*.py"):
        assert "volt_drive_backup" not in py.read_text(encoding="utf-8", errors="ignore"), py
