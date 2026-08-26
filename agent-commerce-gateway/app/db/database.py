"""
Database Infrastructure — Agent Commerce Gateway
=================================================

Provides a SQLAlchemy engine and session factory backed by SQLite.

Design decisions:
    - A single `get_engine()` call returns (or creates) the engine for a given
      database URL.  Callers may pass an in-memory URL for isolated tests.
    - `init_db(engine)` creates all tables declared on `Base`.
    - Sessions are managed by callers via context managers; this module does
      not hold any long-lived global state other than the engine cache.

SQLite notes:
    - `check_same_thread=False` is required because FastAPI / pytest may
      access the same connection from multiple threads.  SQLAlchemy's
      connection pool handles thread safety correctly.
    - WAL journal mode is enabled for better concurrent read/write performance.
    - Foreign-key enforcement is enabled per connection.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Declarative base — all ORM models inherit from this
# ──────────────────────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Engine factory
# ──────────────────────────────────────────────────────────────────────────────

if os.environ.get("VERCEL"):
    _DEFAULT_DB_URL = "sqlite:////tmp/gateway.db"
else:
    _DEFAULT_DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/gateway.db")


def get_engine(db_url: str = _DEFAULT_DB_URL) -> Engine:
    """
    Return a SQLAlchemy Engine for the given SQLite URL.

    Args:
        db_url: SQLAlchemy-compatible database URL.
                Defaults to a local file-based SQLite DB.
                Pass ``"sqlite:///:memory:"`` for fully isolated in-memory tests.

    Returns:
        A configured SQLAlchemy Engine.
    """
    if db_url.startswith("sqlite:///") and not db_url.startswith("sqlite:///:memory:"):
        db_file = db_url.replace("sqlite:///", "")
        dir_path = os.path.dirname(os.path.abspath(db_file))
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        # echo=False keeps SQL out of normal logs; set True for debugging.
        echo=False,
    )

    # Enable WAL mode and foreign-key enforcement for every connection.
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_conn, _connection_record):  # type: ignore[misc]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    logger.debug("SQLite engine created: %s", db_url)
    return engine


def init_db(engine: Engine) -> None:
    """
    Create all tables defined on `Base` if they do not yet exist.

    Safe to call multiple times (CREATE TABLE IF NOT EXISTS semantics).

    Args:
        engine: An active SQLAlchemy Engine.
    """
    Base.metadata.create_all(engine)
    logger.debug("Database tables initialised.")


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """
    Return a session factory bound to the given engine.

    Args:
        engine: An active SQLAlchemy Engine.

    Returns:
        A sessionmaker that produces Session objects.
    """
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)
