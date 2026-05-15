"""Shared SQLAlchemy session/engine helpers.

Compass uses a single SQLite file. We expose:
    engine    - SQLAlchemy Engine
    Session   - scoped session factory
    Base      - declarative base for all models
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import Settings


class Base(DeclarativeBase):
    """All Compass ORM models inherit from this."""


_engine = None
_SessionFactory: sessionmaker[Session] | None = None


def init_engine(settings: Settings) -> None:
    """Initialize engine + session factory from settings. Idempotent."""
    global _engine, _SessionFactory
    if _engine is not None:
        return
    # SQLite default busy_timeout=0 → "database is locked" errors on contention.
    # Long resume_writer pipelines flush many rows; if another connection is
    # writing simultaneously the second one fails immediately. We:
    #   - set sqlite3 connect timeout (driver-level wait when busy)
    #   - enable WAL mode (better concurrent reader+writer)
    #   - PRAGMA busy_timeout (engine-level retry-on-lock for 30s)
    is_sqlite = settings.db_url.startswith("sqlite")
    connect_args = {"timeout": 30.0, "check_same_thread": False} if is_sqlite else {}
    _engine = create_engine(settings.db_url, future=True, echo=False, connect_args=connect_args)
    if is_sqlite:
        from sqlalchemy import event
        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    _SessionFactory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


def get_engine():
    if _engine is None:
        raise RuntimeError("engine not initialized — call init_engine() first")
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context.

    Commits on clean exit, rolls back on exception. Always closes.
    """
    if _SessionFactory is None:
        raise RuntimeError("session factory not initialized — call init_engine() first")
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
