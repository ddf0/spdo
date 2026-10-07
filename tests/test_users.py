"""Users: passwords, service, auth routes, roles, CSRF, log redaction."""

import logging
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from spdo import logsafe
from spdo.users import service
from spdo.users.models import Role
from spdo.users.routes import safe_next
from spdo.users.security import PasswordPolicyError, hash_password, verify_password

PW = "correct-horse-1"


# --- passwords -------------------------------------------------------------


def test_hash_is_salted_and_verifies() -> None:
    a, b = hash_password(PW), hash_password(PW)
    assert a != b and a.startswith("$2b$")
    assert verify_password(PW, a)
    assert not verify_password("wrong-password", a)


@pytest.mark.parametrize("bad", ["short", "я" * 37])  # 7 chars; 74 bytes
def test_password_policy(bad: str) -> None:
    with pytest.raises(PasswordPolicyError):
        hash_password(bad)


def test_verify_rejects_overlong_and_garbage_hash() -> None:
    assert not verify_password("x" * 100, hash_password(PW))
    assert not verify_password(PW, "not-a-hash")


# --- service ---------------------------------------------------------------


def test_create_and_authenticate(session: Session) -> None:
    u = service.create_user(session, username="  Ivan.P ", full_name="", password=PW)
    assert u.username == "ivan.p" and u.full_name == "ivan.p" and u.role is Role.USER
    assert u.password_hash != PW
    assert service.authenticate(session, "IVAN.P", PW) is u
    assert service.authenticate(session, "ivan.p", "wrong-password") is None
    assert service.authenticate(session, "nobody", PW) is None


@pytest.mark.parametrize("name", ["ab", "иван", "a b", "x" * 65])
def test_invalid_username(session: Session, name: str) -> None:
    with pytest.raises(service.InvalidUsernameError):
        service.create_user(session, username=name, full_name="", password=PW)


def test_duplicate_username(session: Session) -> None:
    service.create_user(session, username="dup", full_name="", password=PW)
    with pytest.raises(service.UsernameTakenError):
        service.create_user(session, username="DUP", full_name="", password=PW)


def test_inactive_user_cannot_login(session: Session) -> None:
    u = service.create_user(session, username="off", full_name="", password=PW)
    service.create_user(session, username="adm", full_name="", password=PW, role=Role.ADMIN)
    service.update_user(session, u.id, is_active=False)
    assert service.authenticate(session, "off", PW) is None
    assert service.get_active_user(session, u.id) is None


def test_last_admin_protected(session: Session) -> None:
    a = service.create_user(session, username="root", full_name="", password=PW, role=Role.ADMIN)
    with pytest.raises(service.LastAdminError):
        service.update_user(session, a.id, role=Role.OPERATOR)
    with pytest.raises(service.LastAdminError):
        service.update_user(session, a.id, is_active=False)
    b = service.create_user(session, username="root2", full_name="", password=PW, role=Role.ADMIN)
    service.update_user(session, a.id, role=Role.OPERATOR, full_name=" New ")
    assert a.role is Role.OPERATOR and a.full_name == "New"
    with pytest.raises(service.LastAdminError):
        service.update_user(session, b.id, is_active=False)


def test_set_password_and_not_found(session: Session) -> None:
    u = service.create_user(session, username="pwd", full_name="", password=PW)
    service.set_password(session, u.id, "another-pass-2")
    assert service.authenticate(session, "pwd", "another-pass-2") is u
    with pytest.raises(service.UserNotFoundError):
        service.set_password(session, 999999, PW)
    with pytest.raises(service.UserNotFoundError):
        service.update_user(session, 999999, role=Role.USER)


# --- routes ----------------------------------------------------------------


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m
    return m.group(1)


def login(client: TestClient, username: str, password: str = PW, next: str = "/"):
    token = csrf_from(client.get("/login").text)
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token, "next": next},
        follow_redirects=False,
    )


def meta_csrf(client: TestClient) -> str:
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m
    return m.group(1)


@pytest.fixture
def admin(session: Session):
    return service.create_user(
        session, username="admin", full_name="Админ", password=PW, role=Role.ADMIN
    )


@pytest.fixture
def plain(session: Session):
    return service.create_user(session, username="user", full_name="Юзер", password=PW)


def test_anonymous_redirects_and_401(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/"
    assert client.get("/api/me").status_code == 401


def test_login_flow(client: TestClient, plain) -> None:
    r = login(client, "user", next="/somewhere")
    assert r.status_code == 303 and r.headers["location"] == "/somewhere"
    assert client.get("/api/me").json()["username"] == "user"
    assert "Юзер" in client.get("/").text

    token = meta_csrf(client)
    r = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/me").status_code == 401


def test_login_wrong_password(client: TestClient, plain) -> None:
    r = login(client, "user", "wrong-password")
    assert r.status_code == 401 and "Неверное имя" in r.text
    assert PW not in r.text


def test_login_requires_csrf(client: TestClient, plain) -> None:
    client.get("/login")
    r = client.post("/login", data={"username": "user", "password": PW})
    assert r.status_code == 403


def test_session_id_rotates_on_login(client: TestClient, plain) -> None:
    before = csrf_from(client.get("/login").text)
    login(client, "user")
    assert meta_csrf(client) != before


def test_open_redirect_blocked(client: TestClient, plain) -> None:
    r = login(client, "user", next="//evil.example")
    assert r.headers["location"] == "/"


@pytest.mark.parametrize(
    ("target", "expected"),
    [("/a?b=1", "/a?b=1"), ("//x", "/"), ("/\\x", "/"), ("http://x", "/"), ("", "/"), (None, "/")],
)
def test_safe_next(target, expected) -> None:
    assert safe_next(target) == expected


def test_admin_api_forbidden_for_user(client: TestClient, plain) -> None:
    login(client, "user")
    assert client.get("/api/admin/users").status_code == 403


def test_admin_api(client: TestClient, admin) -> None:
    login(client, "admin")
    h = {"X-CSRF-Token": meta_csrf(client)}

    assert (
        client.post("/api/admin/users", json={"username": "oper", "password": PW}).status_code == 403
    )

    r = client.post(
        "/api/admin/users",
        json={"username": "oper", "full_name": "Оператор", "password": PW, "role": "operator"},
        headers=h,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "operator" and "password_hash" not in body
    op_id = body["id"]

    assert (
        client.post(
            "/api/admin/users", json={"username": "oper", "password": PW}, headers=h
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/admin/users", json={"username": "x y", "password": PW}, headers=h
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/admin/users", json={"username": "short", "password": "1"}, headers=h
        ).status_code
        == 400
    )

    assert [u["username"] for u in client.get("/api/admin/users").json()] == ["admin", "oper"]

    r = client.patch(f"/api/admin/users/{op_id}", json={"is_active": False}, headers=h)
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert client.patch("/api/admin/users/999999", json={}, headers=h).status_code == 404
    assert (
        client.patch(f"/api/admin/users/{admin.id}", json={"role": "user"}, headers=h).status_code
        == 409
    )

    assert (
        client.put(
            f"/api/admin/users/{op_id}/password", json={"password": "new-pass-123"}, headers=h
        ).status_code
        == 204
    )
    assert (
        client.put(
            f"/api/admin/users/{op_id}/password", json={"password": "1"}, headers=h
        ).status_code
        == 400
    )
    assert (
        client.put("/api/admin/users/999999/password", json={"password": PW}, headers=h).status_code
        == 404
    )


def test_deactivated_user_session_dropped(
    client: TestClient, session: Session, plain, admin
) -> None:
    login(client, "user")
    service.update_user(session, plain.id, is_active=False)
    assert client.get("/api/me").status_code == 401


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


# --- logs ------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "POST /login?password=hunter22&x=1",
        "Cookie: spdo_session=abc.def.ghi",
        '{"password": "hunter22"}',
        "csrf_token=tok123",
        "Authorization: Bearer xyz",
    ],
)
def test_redact(line: str) -> None:
    out = logsafe.redact(line)
    assert "hunter22" not in out and "abc.def" not in out and "tok123" not in out
    assert logsafe.MASK in out


def test_filter_on_handler(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("spdo.test")
    caplog.handler.addFilter(logsafe.RedactSecretsFilter())
    log.warning("login with password=%s", "hunter22")
    assert "hunter22" not in caplog.text
    log.warning("plain message %s", 1)
    assert "plain message 1" in caplog.text


def test_install_is_idempotent() -> None:
    handler = logging.StreamHandler()
    logging.getLogger("spdo.idem").addHandler(handler)
    logsafe.install(("spdo.idem",))
    logsafe.install(("spdo.idem",))
    assert sum(isinstance(f, logsafe.RedactSecretsFilter) for f in handler.filters) == 1


# --- review fixes ----------------------------------------------------------


def test_unset_secret_key_is_randomized() -> None:
    from spdo.config import Settings

    a, b = Settings(secret_key=""), Settings(secret_key="change-me")
    assert a.secret_key_generated and b.secret_key_generated
    assert a.secret_key not in ("", "change-me") and a.secret_key != b.secret_key
    c = Settings(secret_key="explicit-key-value")
    assert not c.secret_key_generated and c.secret_key == "explicit-key-value"


def test_concurrent_create_maps_to_taken(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    service.create_user(session, username="race", full_name="", password=PW)
    # simulate the other request winning between pre-check and insert
    monkeypatch.setattr(session, "scalar", lambda *a, **k: None)
    with pytest.raises(service.UsernameTakenError):
        service.create_user(session, username="race", full_name="", password=PW)


def test_non_ascii_csrf_is_403(client: TestClient, admin) -> None:
    login(client, "admin")
    r = client.post("/api/admin/users", json={"username": "abc", "password": PW}, headers={"X-CSRF-Token": b"\xe9"})
    assert r.status_code == 403


def test_admin_api_unauthenticated_is_401(client: TestClient) -> None:
    assert client.post("/api/admin/users", json={}).status_code == 401


def test_update_rejects_empty_full_name(client: TestClient, admin) -> None:
    login(client, "admin")
    h = {"X-CSRF-Token": meta_csrf(client)}
    assert client.patch(f"/api/admin/users/{admin.id}", json={"full_name": ""}, headers=h).status_code == 422
    big = {"username": "abc", "password": "x" * 300}
    assert client.post("/api/admin/users", json=big, headers=h).status_code == 422


def test_security_headers(client: TestClient, plain) -> None:
    r = client.get("/login")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "cache-control" not in client.get("/static/app.css").headers


@pytest.mark.parametrize(
    ("line", "leak"),
    [
        ("password=a b", "a b"),
        ('{"password": "p w d"}', "p w d"),
        ("Authorization: Bearer xyz123", "xyz123"),
        ("password_hash='$2b$04$abc'", "$2b$04$abc"),
        ("X-CSRF-Token: tok999", "tok999"),
    ],
)
def test_redact_extended(line: str, leak: str) -> None:
    assert leak not in logsafe.redact(line)


def test_filter_redacts_traceback(caplog: pytest.LogCaptureFixture) -> None:
    caplog.handler.addFilter(logsafe.RedactSecretsFilter())
    try:
        raise ValueError("bad password=hunter22")
    except ValueError:
        logging.getLogger("spdo.test").exception("failed")
    assert "hunter22" not in caplog.text
