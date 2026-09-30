"""Operator authentication: hashing, signed sessions, lockout, route guard.

These tests exist because the console can execute actions and wipe the demo.
An auth bug is therefore a data-loss bug, not just a cosmetic one.
"""

from __future__ import annotations

import time

import pytest

from recallops.services import auth


# --------------------------------------------------------------------------- passwords


def test_password_hash_is_salted_and_never_reversible():
    encoded = auth.hash_password("correct horse battery staple")
    assert encoded.startswith("pbkdf2_sha256$")
    assert "correct horse" not in encoded

    # Two hashes of the same password must differ (unique salt).
    assert auth.hash_password("same") != auth.hash_password("same")


def test_verify_password_accepts_only_the_right_secret():
    encoded = auth.hash_password("s3cret!")
    assert auth.verify_password("s3cret!", encoded) is True
    assert auth.verify_password("s3cret", encoded) is False
    assert auth.verify_password("", encoded) is False


def test_verify_password_rejects_malformed_stored_hashes():
    # A corrupt row must not raise - it must simply fail closed.
    for bad in ("", "nonsense", "pbkdf2_sha256$abc", "md5$1$a$b", "pbkdf2_sha256$x$y$z"):
        assert auth.verify_password("anything", bad) is False


def test_generated_passwords_are_long_and_not_obvious():
    pwd = auth.generate_password()
    assert len(pwd) >= 20
    assert pwd not in ("admin", "password", "changeme")
    assert pwd != auth.generate_password()


# --------------------------------------------------------------------------- tokens


def test_token_round_trips_within_ttl():
    token = auth.issue_token("alice", ttl_s=60)
    assert auth.read_token(token) == "alice"


def test_token_is_rejected_after_expiry():
    token = auth.issue_token("alice", ttl_s=-1)  # already expired
    assert auth.read_token(token) is None


def test_tampered_token_signature_is_rejected():
    token = auth.issue_token("alice", ttl_s=60)
    payload, _, sig = token.rpartition(".")
    forged = f"{payload}.{'A' * len(sig)}"
    assert auth.read_token(forged) is None
    # A different username must not be smuggled in under a valid signature.
    assert auth.read_token(f"{auth.issue_token('bob', 60).split('.')[0]}.{sig}") is None


def test_garbage_token_is_rejected():
    for bad in ("", "abc", "a.b", "...", "no-dot-at-all"):
        assert auth.read_token(bad) is None


# --------------------------------------------------------------------------- store


def test_bootstrap_creates_one_operator_and_is_idempotent(clean_db, monkeypatch):
    from recallops.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_username", "sre-oncall", raising=False)
    monkeypatch.setattr(get_settings(), "auth_password", "hunter2-very-long", raising=False)

    assert auth.count_users() == 0
    auth.bootstrap_operator()
    assert auth.count_users() == 1
    # Second call must not create a duplicate.
    auth.bootstrap_operator()
    assert auth.count_users() == 1
    assert auth.find_user("sre-oncall") is not None


def test_authenticate_accepts_correct_credentials(clean_db, monkeypatch):
    from recallops.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_username", "op", raising=False)
    monkeypatch.setattr(get_settings(), "auth_password", "letmein-1234", raising=False)
    auth.bootstrap_operator()

    ok, reason = auth.authenticate("op", "letmein-1234")
    assert ok is True
    assert reason == "ok"


def test_authenticate_rejects_wrong_password_and_unknown_user(clean_db):
    auth.create_user("op", "right-password")
    ok, reason = auth.authenticate("op", "wrong-password")
    assert ok is False
    assert "Invalid" in reason

    # Unknown user must be indistinguishable from a wrong password.
    ok2, reason2 = auth.authenticate("ghost", "whatever")
    assert ok2 is False
    assert reason == reason2


def test_repeated_failures_lock_the_account(clean_db):
    auth.create_user("op", "right-password")
    for _ in range(auth._LOCKOUT_AFTER):
        ok, reason = auth.authenticate("op", "nope")
        assert ok is False
    locked, message = auth.authenticate("op", "right-password")
    assert locked is False
    assert "Too many" in message


def test_inactive_operator_cannot_sign_in(clean_db):
    auth.create_user("op", "right-password")
    user = auth.find_user("op")
    session_user = user
    assert session_user is not None
    # Deactivate directly through a session to avoid a dedicated admin API.
    from recallops.persistence import models as orm
    from recallops.persistence.db import get_sessionmaker

    s = get_sessionmaker()()
    try:
        row = s.query(orm.OperatorUser).filter(orm.OperatorUser.username == "op").one()
        row.is_active = False
        s.commit()
    finally:
        s.close()

    ok, _ = auth.authenticate("op", "right-password")
    assert ok is False


# --------------------------------------------------------------------------- registration


def test_validate_registration_accepts_a_sensible_account(clean_db):
    ok, reason = auth.validate_registration("sre-oncall", "correct-horse", "Payments on-call")
    assert ok is True
    assert reason == "ok"


def test_validate_registration_rejects_weak_input(clean_db):
    cases = [
        ("ab", "correct-horse", "too short a username"),
        ("has space", "correct-horse", "illegal characters"),
        ("ok.name", "short", "password too short"),
        ("ok.name", "ok.name", "password equals username"),
    ]
    for username, password, why in cases:
        ok, reason = auth.validate_registration(username, password)
        assert ok is False, why
        assert reason


def test_validate_registration_rejects_a_taken_username(clean_db):
    """A taken username is a conflict (409), not a malformed request (400), so
    validate_registration must let it through for the route to convert."""
    auth.create_user("taken", "correct-horse")
    ok, _ = auth.validate_registration("taken", "another-one")
    assert ok is True
    with pytest.raises(auth.UsernameTaken):
        auth.create_user("taken", "yet-another")


def test_registration_can_be_disabled(clean_db, monkeypatch):
    from recallops.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_registration_enabled", False, raising=False)
    ok, reason = auth.validate_registration("newbie", "correct-horse")
    assert ok is False
    assert "disabled" in reason


def test_create_user_raises_on_duplicate(clean_db):
    auth.create_user("dup", "correct-horse")
    with pytest.raises(auth.UsernameTaken):
        auth.create_user("dup", "another-password")


def test_register_endpoint_creates_an_account_and_signs_in(clean_db):
    auth.create_user("bootstrap-op", "bootstrap-pass")
    response = client_post_register("new-op", "brand-new-pass", "New Operator", "new.op@company.com")
    assert response.status_code == 201
    body = response.json()
    assert body["authenticated"] is True
    assert body["user"]["username"] == "new-op"
    assert body["user"]["full_name"] == "New Operator"
    assert body["user"]["email"] == "new.op@company.com"
    assert auth.COOKIE_NAME in response.cookies
    assert "httponly" in response.headers["set-cookie"].lower()
    # The new account is immediately usable, by username *and* by email.
    assert auth.authenticate("new-op", "brand-new-pass")[0] is True
    assert auth.authenticate("new.op@company.com", "brand-new-pass")[0] is True


def client_post_register(username: str, password: str, full_name: str = "", email: str | None = None):
    from fastapi.testclient import TestClient

    from recallops.main import app

    payload = {
        "full_name": full_name,
        "email": email if email is not None else f"{username}@company.com",
        "username": username,
        "password": password,
    }
    with TestClient(app) as c:
        return c.post("/api/auth/register", json=payload)


def test_register_endpoint_rejects_duplicates_with_409(clean_db):
    auth.create_user("dup", "correct-horse", email="dup@company.com")
    response = client_post_register("dup", "another-password", email="dup@company.com")
    assert response.status_code == 409
    assert "already" in response.json()["detail"]


def test_register_endpoint_rejects_weak_passwords_with_400(clean_db):
    response = client_post_register("someone", "short")
    assert response.status_code == 400
    assert "at least" in response.json()["detail"]


def test_register_endpoint_rejects_a_malformed_email(clean_db):
    response = client_post_register("someone", "correct-horse", email="not-an-email")
    assert response.status_code == 400
    assert "valid email" in response.json()["detail"]


def test_duplicate_email_is_rejected_even_with_a_new_username(clean_db):
    """Email is the sign-in identity, so it must be unique across accounts."""
    auth.create_user("first", "correct-horse", email="shared@company.com")
    response = client_post_register("second", "correct-horse", email="shared@company.com")
    assert response.status_code == 409
    assert "email" in response.json()["detail"].lower()


def test_email_login_is_case_insensitive(clean_db):
    auth.create_user("amaya", "correct-horse", email="Amaya@Company.com")
    assert auth.authenticate("amaya@company.com", "correct-horse")[0] is True
    assert auth.authenticate("AMAYA@COMPANY.COM", "correct-horse")[0] is True


def test_account_without_an_email_can_still_sign_in_by_username(clean_db):
    """The bootstrap operator predates email; it must not be locked out."""
    auth.create_user("legacy", "correct-horse")
    ok, _ = auth.authenticate("legacy", "correct-horse")
    assert ok is True


def test_me_endpoint_returns_the_operator_without_secrets(clean_db):
    auth.create_user("me-op", "correct-horse", "Me Operator", "me@company.com")
    from fastapi.testclient import TestClient

    from recallops.main import app

    with TestClient(app) as c:
        assert c.get("/api/auth/me").status_code in (200, 401)  # open mode allows anonymous
        c.post("/api/auth/login", json={"email": "me@company.com", "password": "correct-horse"})
        body = c.get("/api/auth/me")
        assert body.status_code == 200
        payload = body.json()
        assert payload["authenticated"] is True
        assert payload["user"]["email"] == "me@company.com"
        # No hash, no internal id, ever.
        assert "password_hash" not in str(payload)
        assert "pbkdf2" not in str(payload)


def test_me_requires_a_session_when_auth_is_required(clean_db, monkeypatch):
    from fastapi.testclient import TestClient

    from recallops.api.deps import set_container
    from recallops.config import get_settings

    import importlib

    import recallops.main as main_mod

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_secret", "test-secret", raising=False)
    importlib.reload(main_mod)
    app = main_mod.create_app()
    set_container(None)

    with TestClient(app) as c:
        assert c.get("/api/auth/me").status_code == 401
        auth.create_user("guard", "correct-horse", email="guard@company.com")
        c.post("/api/auth/login", json={"email": "guard@company.com", "password": "correct-horse"})
        assert c.get("/api/auth/me").status_code == 200


def test_register_is_reachable_when_auth_is_required(clean_db, monkeypatch):
    """Sign-up must not be caught by its own auth guard."""
    from fastapi.testclient import TestClient

    from recallops.api.deps import set_container
    from recallops.config import get_settings

    import importlib

    import recallops.main as main_mod

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_secret", "test-secret", raising=False)
    monkeypatch.setattr(settings, "auth_registration_enabled", True, raising=False)
    importlib.reload(main_mod)
    app = main_mod.create_app()
    set_container(None)

    with TestClient(app) as c:
        assert c.post("/api/auth/register", json={"full_name": "Anon Op", "email": "anon@company.com", "username": "anon-op", "password": "correct-horse"}).status_code == 201
        # And the freshly created operator can immediately use the console.
        assert c.get("/api/incidents").status_code == 200


def test_session_endpoint_advertises_registration_availability(client):
    body = client.get("/api/auth/session").json()
    assert "registration_enabled" in body
    assert "min_password_length" in body


# --------------------------------------------------------------------------- CORS


def test_production_vercel_origin_is_allowed():
    """The deployed console is on Vercel; the API must accept it, and a preflight
    must come back with the right origin rather than a 400."""
    from fastapi.testclient import TestClient

    from recallops.main import app

    with TestClient(app) as c:
        for origin in ("https://recall-ops-five.vercel.app", "https://recall-ops-abc123.vercel.app"):
            r = c.options(
                "/api/incidents",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
            assert r.status_code == 200, origin
            assert r.headers.get("access-control-allow-origin") == origin


def test_untrusted_public_origin_is_still_rejected():
    from fastapi.testclient import TestClient

    from recallops.main import app

    with TestClient(app) as c:
        r = c.options(
            "/api/incidents",
            headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST"},
        )
        assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_cors_preflight_survives_auth_when_enabled(clean_db, monkeypatch):
    """Regression: the session guard sits outside CORSMiddleware, so a preflight
    must be exempted from it.

    A browser's OPTIONS probe carries no cookies. If the guard answered 401
    instead of letting CORS reply, the real request would never leave the browser
    and the console would look broken with no frontend error at all.
    """
    from fastapi.testclient import TestClient

    from recallops.api.deps import set_container
    from recallops.config import get_settings

    import importlib

    import recallops.main as main_mod

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_secret", "test-secret", raising=False)
    importlib.reload(main_mod)
    app = main_mod.create_app()
    set_container(None)

    with TestClient(app) as c:
        for origin in ("https://recall-ops-five.vercel.app", "https://recall-ops-abc.vercel.app", "http://localhost:4321"):
            preflight = c.options(
                "/api/incidents",
                headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"},
            )
            assert preflight.status_code == 200, f"preflight blocked for {origin}: {preflight.status_code}"
            assert preflight.headers.get("access-control-allow-origin") == origin
            assert preflight.headers.get("access-control-allow-credentials") == "true"

        # An untrusted origin gets no CORS grant (it may 401, but must not be allowed).
        blocked = c.options(
            "/api/incidents",
            headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST"},
        )
        assert blocked.headers.get("access-control-allow-origin") != "https://evil.example.com"

        # The actual request is still protected.
        assert c.get("/api/incidents").status_code == 401


def test_unauthenticated_401_still_carries_cors_headers(clean_db, monkeypatch):
    """Regression: a 401 produced by the session guard must still be readable by
    the browser.

    CORSMiddleware has to be the outermost middleware. When it is registered
    first it ends up innermost, the guard's own 401 bypasses it, and the response
    arrives with no Access-Control-Allow-Origin - which the browser reports as an
    opaque CORS failure, so the frontend never sees the 401 it needs in order to
    redirect to /login.
    """
    from fastapi.testclient import TestClient

    from recallops.api.deps import set_container
    from recallops.config import get_settings

    import importlib

    import recallops.main as main_mod

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_secret", "test-secret", raising=False)
    importlib.reload(main_mod)
    app = main_mod.create_app()
    set_container(None)

    origin = "https://recall-ops-five.vercel.app"
    with TestClient(app) as c:
        r = c.get("/api/incidents", headers={"Origin": origin})
        assert r.status_code == 401
        assert r.headers.get("access-control-allow-origin") == origin, "401 lost its CORS headers"
        assert r.headers.get("access-control-allow-credentials") == "true"


def test_lookalike_vercel_domain_is_not_allowed():
    """A hostname that merely contains 'vercel.app' must not inherit the grant."""
    from fastapi.testclient import TestClient

    from recallops.main import app

    with TestClient(app) as c:
        r = c.options(
            "/api/incidents",
            headers={"Origin": "https://vercel-app.attacker.com", "Access-Control-Request-Method": "POST"},
        )
        assert r.headers.get("access-control-allow-origin") != "https://vercel-app.attacker.com"


# --------------------------------------------------------------------------- public paths


def test_health_and_login_stay_public():
    for path in ("/health", "/health/live", "/health/ready", "/api/auth/login", "/api/auth/register", "/api/auth/session", "/docs", "/openapi.json"):
        assert auth.is_public_path(path) is True, path


def test_data_routes_are_not_public():
    for path in ("/", "/api/incidents", "/api/demo/reset", "/api/analytics", "/api/memory", "/api/comparison/INC-A1/run"):
        assert auth.is_public_path(path) is False, path


# --------------------------------------------------------------------------- HTTP guard


def test_open_mode_allows_everything(client):
    """Default mode (AUTH_REQUIRED=false) must keep the demo and tests working."""
    assert client.get("/api/auth/session").json()["auth_required"] is False
    assert client.get("/api/incidents").status_code == 200


def test_session_endpoint_reports_configured_state(client):
    body = client.get("/api/auth/session").json()
    assert body["auth_required"] is False
    assert "authenticated" in body
    assert "operators_configured" in body


def test_login_rejects_bad_credentials_with_401(client):
    auth.create_user("op", "right-password")
    response = client.post("/api/auth/login", json={"email": "op", "password": "wrong"})
    assert response.status_code == 401
    assert "Invalid" in response.json()["detail"]


def test_login_sets_httponly_cookie(client):
    auth.create_user("op", "right-password")
    response = client.post("/api/auth/login", json={"email": "op", "password": "right-password"})
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert auth.COOKIE_NAME in response.cookies
    # The whole point of the cookie: script must not be able to read it.
    assert "httponly" in response.headers["set-cookie"].lower()


def test_logout_clears_the_cookie(client):
    auth.create_user("op", "right-password")
    client.post("/api/auth/login", json={"email": "op", "password": "right-password"})
    response = client.post("/api/auth/logout")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    assert "recallops_session=" in response.headers["set-cookie"]


def test_auth_required_mode_blocks_anonymous_access(clean_db, monkeypatch):
    """The regression that matters: with auth on, nothing is served anonymously."""
    from fastapi.testclient import TestClient

    from recallops.api.deps import set_container
    from recallops.config import get_settings
    from recallops.main import create_app

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_secret", "test-secret-key", raising=False)

    # The app is built fresh so the middleware closure sees AUTH_REQUIRED=true.
    import importlib

    import recallops.main as main_mod

    importlib.reload(main_mod)
    app = main_mod.create_app()
    set_container(None)

    with TestClient(app) as c:
        # Public surface still works (Render health checks must not break).
        assert c.get("/health/live").status_code == 200
        assert c.get("/health/ready").status_code == 200
        assert c.get("/api/auth/session").status_code == 200

        # Everything else is closed.
        assert c.get("/api/incidents").status_code == 401
        assert c.get("/api/analytics").status_code == 401
        assert c.post("/api/demo/reset").status_code == 401

        # Wrong password still cannot get in.
        assert c.post("/api/auth/login", json={"email": "op", "password": "nope"}).status_code in (401, 429)

        # Right password opens it up, including state-changing routes.
        auth.create_user("op", "right-password")
        assert c.post("/api/auth/login", json={"email": "op", "password": "right-password"}).status_code == 200
        assert c.get("/api/incidents").status_code == 200

        # And signing out closes it again.
        c.post("/api/auth/logout")
        assert c.get("/api/analytics").status_code == 401
