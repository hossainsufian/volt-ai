"""Remote control: a Volt Code terminal session shared with the Volt app (/rc)."""

import threading
import time

import pytest

from desktop_app.volt_rc import RemoteSessions
from desktop_app.volt_store import VoltStore
from desktop_app.volt_web import VoltWebServer

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
PHONE = {"REMOTE_ADDR": "100.108.60.63"}


class Bridge:
    def is_available(self): return True
    def submit(self, text): return True
    def cancel(self): pass
    def restore(self, messages): pass


# --- the session registry --------------------------------------------------------------

def test_events_are_kept_in_order_for_viewers():
    rc = RemoteSessions()
    sid = rc.create("Fix tests", "/home/me/app", "laptop")
    rc.add_events(sid, [{"type": "text", "text": "Hello"}, {"type": "tool", "name": "read_file"}])
    events = rc.events(sid, after=0)
    assert [e["seq"] for e in events] == [1, 2]
    assert rc.events(sid, after=1)[0]["name"] == "read_file"


def test_inbox_waits_for_the_phone_and_delivers_once():
    rc = RemoteSessions()
    sid = rc.create("t", "/x", "h")
    threading.Timer(0.1, lambda: rc.send(sid, {"text": "add a test"})).start()
    started = time.monotonic()
    assert rc.take_inbox(sid, wait=2) == [{"text": "add a test"}]
    assert time.monotonic() - started < 1.5
    assert rc.take_inbox(sid, wait=0) == []


def test_session_goes_offline_when_volt_code_stops_checking_in(monkeypatch):
    rc = RemoteSessions(offline_after=0.05)
    sid = rc.create("t", "/x", "h")
    assert rc.list()[0]["online"]
    time.sleep(0.1)
    assert not rc.list()[0]["online"]
    rc.take_inbox(sid, wait=0)  # checking in brings it back
    assert rc.list()[0]["online"]


# --- over the web server ----------------------------------------------------------------

@pytest.fixture
def web(tmp_path):
    srv = VoltWebServer(VoltStore(tmp_path), Bridge(), port=0)
    srv.settings["phone_access"] = True
    srv.set_phone_password("volt-home-42")
    term = srv.app.test_client()   # Volt Code, signed in with the phone password
    term.post("/api/pair", json={"password": "volt-home-42"}, environ_base=PHONE)
    phone = srv.app.test_client()
    phone.post("/api/pair", json={"password": "volt-home-42"}, environ_base=PHONE)
    return srv, term, phone


def test_phone_sees_the_session_and_can_answer(web):
    srv, term, phone = web
    sid = term.post("/api/rc/sessions", json={"title": "quiz app", "folder": "/home/me/quiz",
                                              "host": "main-pc"}, environ_base=PHONE).get_json()["id"]
    term.post(f"/api/rc/sessions/{sid}/events", json={"events": [
        {"type": "approval", "id": "a1", "title": "Create index.html", "detail": "<html>"}]}, environ_base=PHONE)
    listed = phone.get("/api/rc/sessions", environ_base=PHONE).get_json()["sessions"]
    assert listed[0]["title"] == "quiz app" and listed[0]["online"]
    events = phone.get(f"/api/rc/sessions/{sid}/events?after=0", environ_base=PHONE).get_json()["events"]
    assert events[0]["type"] == "approval"
    phone.post(f"/api/rc/sessions/{sid}/input", json={"approval": "a1", "answer": "yes"}, environ_base=PHONE)
    phone.post(f"/api/rc/sessions/{sid}/input", json={"text": "now add a timer"}, environ_base=PHONE)
    inbox = term.get(f"/api/rc/sessions/{sid}/inbox?wait=0", environ_base=PHONE).get_json()["inbox"]
    assert inbox == [{"approval": "a1", "answer": "yes"}, {"text": "now add a timer"}]


def test_strangers_cannot_see_or_control_sessions(web):
    srv, term, phone = web
    sid = term.post("/api/rc/sessions", json={"title": "t"}, environ_base=PHONE).get_json()["id"]
    stranger = srv.app.test_client()
    assert stranger.get("/api/rc/sessions", environ_base=PHONE).status_code == 401
    assert stranger.post(f"/api/rc/sessions/{sid}/input", json={"text": "rm -rf"},
                         environ_base=PHONE).status_code == 401


def test_bad_input_is_refused(web):
    srv, term, phone = web
    sid = term.post("/api/rc/sessions", json={"title": "t"}, environ_base=PHONE).get_json()["id"]
    assert phone.post(f"/api/rc/sessions/{sid}/input", json={"approval": "a1", "answer": "maybe"},
                      environ_base=PHONE).status_code == 400
    assert phone.post("/api/rc/sessions/nope/input", json={"text": "hi"}, environ_base=PHONE).status_code == 404
