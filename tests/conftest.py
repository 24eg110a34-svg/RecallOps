"""Shared pytest fixtures: an isolated database and container per test."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Set before any recallops import: ``get_settings`` is cached at import time, so a
# session fixture would be too late and the app would auto-seed the demo story.
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_recallops.db")
os.environ.setdefault("LLM_PROVIDER", "local_heuristic")
os.environ.setdefault("HINDSIGHT_ENABLED", "0")
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("DEMO_SEED_ON_START", "false")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:4321")

# Auth must be OFF for the suite, and it must be forced rather than defaulted:
# pydantic-settings also reads the developer's .env file, so a local .env with
# AUTH_REQUIRED=true would otherwise turn the whole suite into 401s. These are
# real assignments, not setdefault, because os.environ wins over the env file.
os.environ["AUTH_REQUIRED"] = "false"
os.environ["AUTH_REGISTRATION_ENABLED"] = "true"
os.environ["AUTH_SECRET"] = "test-only-signing-key"


@pytest.fixture(scope="session", autouse=True)
def _env() -> None:
    """Re-assert the test environment (in case something mutated it)."""
    os.environ["DATABASE_URL"] = os.environ.get("DATABASE_URL", "sqlite:///./test_recallops.db")
    os.environ["LLM_PROVIDER"] = "local_heuristic"
    os.environ["HINDSIGHT_ENABLED"] = "0"
    os.environ["DEMO_SEED_ON_START"] = "false"


@pytest.fixture()
def clean_db():
    from recallops.persistence.db import reset_db

    reset_db()
    yield
    reset_db()


@pytest.fixture()
def container(clean_db):
    from recallops.api.deps import build_container, set_container

    c = build_container()
    set_container(c)
    return c


@pytest.fixture()
def orchestrator(container):
    return container.orchestrator


@pytest.fixture()
def client(container):
    from fastapi.testclient import TestClient

    from recallops.main import app

    with TestClient(app) as c:
        yield c
