"""Phones sign in to Volt AI with a permanent password set on the computer."""

import pytest

from desktop_app.volt_store import VoltStore
from desktop_app.volt_web import VoltWebServer, PAIR_MAX_FAILURES

PHONE = {"REMOTE_ADDR": "100.108.60.63"}
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


class Bridge:
    def is_available(self): return True
    def submit(self, text): return True
    def cancel(self): pass
    def restore(self, messages): pass


@pytest.fixture
def server(tmp_path):
    srv = VoltWebServer(VoltStore(tmp_path), Bridge(), port=0)
    srv.update_settings(phone_access=False)  # no rebind timer in tests
    srv.settings["phone_access"] = True
    return srv


def set_password(server, password):
    client = server.app.test_client()
    client.get(f"/?t={server.desktop_token}", environ_base=LOCAL)
    return client.post("/api/phone-password", json={"password": password}, environ_base=LOCAL)


def phone_login(server, password, client=None):
    client = client or server.app.test_client()
    return client, client.post("/api/pair", json={"password": password}, environ_base=PHONE)


def test_phone_signs_in_with_the_password_and_stays_signed_in(server):
    assert set_password(server, "volt-home-42").status_code == 200
    client, resp = phone_login(server, "volt-home-42")
    assert resp.status_code == 200
    assert client.get("/api/chats", environ_base=PHONE).status_code == 200


def test_password_keeps_working_for_more_phones(server):
    set_password(server, "volt-home-42")
    for _ in range(3):
        _, resp = phone_login(server, "volt-home-42")
        assert resp.status_code == 200


def test_wrong_password_is_refused_and_limited(server):
    set_password(server, "volt-home-42")
    client = server.app.test_client()
    for _ in range(PAIR_MAX_FAILURES):
        _, resp = phone_login(server, "guess", client)
        assert resp.status_code == 403
    _, resp = phone_login(server, "volt-home-42", client)
    assert resp.status_code == 429


def test_no_phone_can_sign_in_before_a_password_is_set(server):
    _, resp = phone_login(server, "")
    assert resp.status_code == 409


def test_short_passwords_are_rejected(server):
    assert set_password(server, "123").status_code == 400


def test_only_the_computer_can_set_the_password(server):
    set_password(server, "volt-home-42")
    client, _ = phone_login(server, "volt-home-42")
    resp = client.post("/api/phone-password", json={"password": "phone-chosen"}, environ_base=PHONE)
    assert resp.status_code == 403


def test_password_is_not_stored_as_plain_text(server, tmp_path):
    set_password(server, "volt-home-42")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"volt-home-42" not in path.read_bytes()


def test_password_survives_a_restart(server, tmp_path):
    set_password(server, "volt-home-42")
    again = VoltWebServer(VoltStore(tmp_path), Bridge(), port=0)
    again.settings["phone_access"] = True
    _, resp = phone_login(again, "volt-home-42")
    assert resp.status_code == 200
