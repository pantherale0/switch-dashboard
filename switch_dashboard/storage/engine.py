import os
import logging
from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from switch_dashboard.config import DATABASE_PATH, ensure_directories

logger = logging.getLogger("switch_dashboard.storage.engine")

_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None
_current_db_url: Optional[str] = None


def get_database_url() -> str:
    """Returns the database URL configured in the environment or falls back

    to the default SQLite database path.
    """
    env_url = os.environ.get("DATABASE_URL", "").strip()
    if env_url:
        # Normalize legacy postgres:// to postgresql:// for SQLAlchemy compatibility
        if env_url.startswith("postgres://"):
            env_url = "postgresql://" + env_url[len("postgres://") :]
        return env_url

    ensure_directories()
    # Normalize Windows and POSIX paths for SQLite
    normalized_path = os.path.abspath(DATABASE_PATH).replace("\\", "/")
    return f"sqlite:///{normalized_path}"


def get_engine(db_url: Optional[str] = None) -> Engine:
    """Returns a singleton SQLAlchemy Engine configured for the active database URL."""
    global _engine, _session_factory, _current_db_url

    url = db_url or get_database_url()

    if _engine is not None and _current_db_url == url:
        return _engine

    logger.info(f"Initializing SQLAlchemy engine for {url.split('@')[-1] if '@' in url else url}")

    connect_args = {}
    engine_kwargs = {"future": True}

    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = 20.0
        engine_kwargs["connect_args"] = connect_args

        if ":memory:" in url:
            engine_kwargs["poolclass"] = StaticPool
        else:
            engine_kwargs["pool_pre_ping"] = True

        engine = create_engine(url, **engine_kwargs)

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode = WAL;")
                cursor.execute("PRAGMA synchronous = NORMAL;")
                cursor.execute("PRAGMA foreign_keys = ON;")
                cursor.execute("PRAGMA busy_timeout = 20000;")
            except Exception as e:
                logger.debug(f"Could not set SQLite pragmas: {e}")
            finally:
                cursor.close()

    else:
        # PostgreSQL, MySQL, MariaDB, etc.
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_size"] = int(os.environ.get("DB_POOL_SIZE", "10"))
        engine_kwargs["max_overflow"] = int(os.environ.get("DB_MAX_OVERFLOW", "20"))
        engine = create_engine(url, **engine_kwargs)

    _engine = engine
    _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    _current_db_url = url
    return _engine


def get_session_factory(db_url: Optional[str] = None) -> sessionmaker:
    """Returns a sessionmaker instance bound to the active engine."""
    get_engine(db_url)
    assert _session_factory is not None
    return _session_factory


@contextmanager
def get_db_session(db_url: Optional[str] = None) -> Generator[Session, None, None]:
    """Context manager for transactional database sessions.

    Automatically commits on normal completion or rolls back on exception.
    """
    factory = get_session_factory(db_url)
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine():
    """Resets the singleton engine and session factory (useful for tests)."""
    global _engine, _session_factory, _current_db_url
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:
            pass
    _engine = None
    _session_factory = None
    _current_db_url = None
