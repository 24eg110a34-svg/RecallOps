"""SQLite engine/session plumbing.

One SQLite file, one schema, created on startup. No migrations framework: the
demo is designed to be reset (``POST /api/demo/reset`` or ``scripts/reset_demo.py``).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from logging import getLogger
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from recallops.config import get_settings

logger = getLogger("recallops.db")


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


REQUIRED_TABLES: tuple[str, ...] = (
    "incidents",
    "incident_events",
    "evidence_events",
    "hypotheses",
    "action_attempts",
    "memory_records",
    "postmortems",
    "operator_users",
)


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
            # 24/7 note: WAL readers must not be starved by a long write, and a
            # busy writer must wait instead of failing the request immediately.
            cur.execute("PRAGMA busy_timeout=30000")
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
    """Create tables if they do not exist, then apply additive column fixes.

    Idempotent. ``create_all`` never *alters* an existing table, so a database
    created before a column was added would otherwise keep the old shape forever
    and the new column would be missing at runtime. Only additive ``ADD COLUMN``
    statements are issued, and only for columns SQLite does not already report -
    no data is dropped or rewritten.
    """
    from recallops.persistence import models  # noqa: F401  (register mappers)

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _apply_additive_columns(engine)


#: Columns added after the first release, applied to pre-existing databases.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (table, column, "TYPE definition")
    ("operator_users", "email", "VARCHAR(255)"),
    ("operator_users", "display_name", "VARCHAR(120) DEFAULT ''"),
)


def _apply_additive_columns(engine: Engine) -> None:
    if not engine.dialect.name == "sqlite":
        return
    with engine.connect() as conn:
        for table, column, ddl in _ADDITIVE_COLUMNS:
            existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing or column in existing:
                continue
            # A UNIQUE constraint cannot be added by ALTER TABLE in SQLite, so the
            # column is added plain; uniqueness is enforced in the auth service and
            # in the fresh schema. Existing rows get NULL, which SQLite allows many
            # times under a non-unique index.
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            conn.commit()
            logger.info("Applied additive migration: %s.%s", table, column)


def drop_db() -> None:
    from recallops.persistence import models  # noqa: F401

    Base.metadata.drop_all(bind=get_engine())


def readiness_probe() -> dict[str, object]:
    """Check that this process can actually serve traffic.

    Three questions, in order of importance for a long-running deployment:

    1. can we *read*? (``SELECT 1``)
    2. can we *write*? a real read-only filesystem is the most common way a
       container or a Windows service starts healthy and then fails on the
       first timeline event, so the write is attempted inside a transaction
       that is always rolled back - no data is created or destroyed
    3. is the schema present? a half-created database must not accept traffic

    Never raises: the caller turns the payload into an HTTP status.
    """
    started = time.perf_counter()
    result: dict[str, object] = {
        "writable": False,
        "schema_ok": False,
        "missing_tables": list(REQUIRED_TABLES),
        "journal_mode": None,
        "db_path": None,
        "error": None,
    }
    path = db_path()
    result["db_path"] = str(path) if path else "in-memory"
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))

            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
            result["journal_mode"] = str(mode) if mode else None

            found = {
                str(row[0])
                for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            }
            missing = [t for t in REQUIRED_TABLES if t not in found]
            result["missing_tables"] = missing
            result["schema_ok"] = not missing

        # Write probe on its own connection so it never collides with the
        # autobegun read transaction above, and always rolled back.
        with get_engine().connect() as writer:
            writer.execute(text("CREATE TABLE IF NOT EXISTS _readiness_probe (id INTEGER PRIMARY KEY, ts REAL)"))
            writer.execute(text("DELETE FROM _readiness_probe"))
            writer.rollback()
        result["writable"] = True
    except Exception as exc:  # noqa: BLE001 - health must never raise
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    result["ready"] = bool(result["writable"] and result["schema_ok"])
    return result


def checkpoint_wal() -> dict[str, object]:
    """Fold the WAL back into the main database file (safe to call anytime).

    Called on shutdown so a restart - or a file copy made by an operator -
    always sees a consistent, self-contained SQLite file.
    """
    try:
        with get_engine().connect() as conn:
            busy = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        return {"ok": True, "detail": str(busy)}
    except Exception as exc:  # noqa: BLE001 - never block shutdown
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}


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
    "REQUIRED_TABLES",
    "checkpoint_wal",
    "db_path",
    "dispose_engine",
    "drop_db",
    "get_db",
    "get_engine",
    "get_sessionmaker",
    "init_db",
    "readiness_probe",
    "reset_db",
    "session_scope",
]
