"""CORS must not break the local demo.

Regression guard: a local demo gets opened as localhost, 127.0.0.1, a LAN address
or a different dev port. Any of those must be accepted; a public host must not be.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recallops.config import get_settings
from recallops.main import app, build_origin_regex


@pytest.fixture()
def cors_client(container):
    with TestClient(app) as c:
        yield c


def _preflight(client: TestClient, origin: str) -> int:
    response = client.options(
        "/api/analytics",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    return response.status_code


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:4321",
        "http://127.0.0.1:4321",
        "http://127.0.0.1:3000",
        "http://localhost:4173",
        "http://[::1]:4321",
        "http://192.168.1.3:4321",
        "http://10.0.0.7:4321",
    ],
)
def test_local_origins_are_allowed(cors_client, origin):
    assert _preflight(cors_client, origin) == 200, f"{origin} was rejected - the demo would show a CORS error"


def test_public_origin_is_rejected(cors_client):
    assert _preflight(cors_client, "http://evil.example.com") == 400


def test_allow_origin_header_echoes_the_request_origin(cors_client):
    response = cors_client.options(
        "/health",
        headers={"Origin": "http://192.168.1.3:4321", "Access-Control-Request-Method": "GET"},
    )
    assert response.headers.get("access-control-allow-origin") == "http://192.168.1.3:4321"


def test_simple_get_request_is_accepted_and_tagged(cors_client):
    response = cors_client.get("/health", headers={"Origin": "http://localhost:4321"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:4321"


def test_origin_regex_covers_loopback_and_lan_only():
    import re

    pattern = build_origin_regex(get_settings())
    assert pattern, "a local demo must accept loopback origins"
    matcher = re.compile(pattern)
    assert matcher.match("http://localhost:4321")
    assert matcher.match("http://192.168.0.9:4321")
    assert not matcher.match("http://evil.example.com")
    assert not matcher.match("https://recallops.attacker.io")
