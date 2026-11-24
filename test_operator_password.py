import os
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

import app
import operator_ui
import store


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    origin = "https://support.example"
    monkeypatch.setenv("INBOX_DB", str(tmp_path / "mail.db"))
    monkeypatch.setenv("INBOX_BASE_URL", origin)
    with TestClient(app.app, base_url=origin) as browser, closing(store.connect()) as db:
        yield browser, db, {"Origin": origin}


def test_password_login_shared_browsers_rotation_and_recovery(inbox):
    browser, db, headers = inbox
    assert 'autocomplete="current-password"' not in browser.get("/").text
    import operator_account

    operator_account.configure(db, "Team@Example.Org", "example fixture password")
    form = {"email": " TEAM@example.org ", "password": "example fixture password", "next": "/investigations"}
    assert 'autocomplete="current-password"' in browser.get("/investigations").text
    assert browser.post("/session/password", data=form).status_code == 403
    invalid = browser.post("/session/password", headers=headers, data=dict(form, password="wrong"))
    assert invalid.status_code == 401 and "Email or password is incorrect" in invalid.text
    login = browser.post("/session/password", headers=headers, data=form, follow_redirects=False)
    assert login.status_code == 303 and login.headers["location"] == "/investigations"
    assert 'httponly' in login.headers['set-cookie'].lower()
    assert 'autocomplete="current-password"' not in browser.get("/investigations").text
    with TestClient(app.app, base_url=os.environ["INBOX_BASE_URL"]) as second:
        assert second.post("/session/password", headers=headers, data=form, follow_redirects=False).status_code == 303
        assert browser.cookies[operator_ui.COOKIE] != second.cookies[operator_ui.COOKIE]
        operator_account.configure(db, "team@example.org", "updated fixture password")
        assert 'autocomplete="current-password"' in browser.get("/investigations").text
        assert 'autocomplete="current-password"' in second.get("/investigations").text
    assert browser.post("/session/password", headers=headers, data=form).status_code == 401
    with store.transaction(db):
        recovery = operator_ui.issue(db, "login", 300)
    assert browser.post("/session", data={"code": recovery}, headers=headers, follow_redirects=False).status_code == 303


def test_password_throttle_across_addresses_and_retry(inbox):
    browser, db, headers = inbox
    import operator_account

    operator_account.configure(db, "team@example.org", "example fixture password")
    for i in range(operator_account.ATTEMPTS):
        res = browser.post("/session/password", headers=headers, data={"email":f"user{i}@example.org","password":"incorrect"})
        assert res.status_code == 401
    form = {"email":"team@example.org","password":"example fixture password"}
    limited = browser.post("/session/password", headers=headers, data=form)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    db.execute("UPDATE operator_password_attempts SET expires=0")
    assert browser.post("/session/password", headers=headers, data=form, follow_redirects=False).status_code == 303
