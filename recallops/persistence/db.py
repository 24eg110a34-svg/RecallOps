"""SQLite engine/session plumbing.

One SQLite file, one schema, created on startup. No migrations framework: the
demo is designed to be reset (``POST /api/demo/reset`` or ``scripts/reset_demo.py``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from recallops.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _build_engine() -> Engine:
    settings = get_settings()
    url = settings.sqlalchemy_url
    kwargs: dict[str, object] = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:  # pragma: no cover - other backends are out of scope but supported
        kwargs.pop("connect_args", None)
    engine = create_engine(url, **kwargs)  # type: ignore[arg-type]

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False, future=True)
    return _SessionLocal


def dispose_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def init_db() -> None:
    """Create tables if they do not exist. Idempotent."""
    from recallops.persistence import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=get_engine())


def drop_db() -> None:
    from recallops.persistence import models  # noqa: F401

    Base.metadata.drop_all(bind=get_engine())


def reset_db() -> None:
    """Destroy and recreate the schema (demo reset)."""
    dispose_engine()
    drop_db()
    init_db()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def db_path() -> Path | None:
    url = get_settings().sqlalchemy_url
    if not url.startswith("sqlite:///"):
        return None
    tail = url[len("sqlite:///") :]
    if tail == ":memory:":
        return None
    p = Path(tail)
    if p.is_absolute():
        return p
    return Path(os.getcwd()) / p


__all__ = [
    "Base",
    "db_path",
    "dispose_engine",
    "drop_db",
    "get_db",
    "get_engine",
    "get_sessionmaker",
    "init_db",
    "reset_db",
    "session_scope",
]
