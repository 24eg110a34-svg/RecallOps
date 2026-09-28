"""Shared pytest fixtures: an isolated database and container per test."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _env() -> None:
    os.environ.setdefault("DATABASE_URL", "sqlite:///./test_recallops.db")
    os.environ.setdefault("LLM_PROVIDER", "local_heuristic")
    os.environ.setdefault("HINDSIGHT_ENABLED", "0")
    os.environ.setdefault("DEMO_MODE", "true")
    os.environ.setdefault("CORS_ORIGINS", "http://localhost:4321")


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
