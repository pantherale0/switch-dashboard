import os
import sqlite3
import logging
from contextlib import contextmanager
from typing import Generator, Optional

from switch_dashboard.config import DATABASE_PATH, ensure_directories

logger = logging.getLogger("switch_dashboard.storage.database")

SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "schema.sql")


class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DATABASE_PATH
        self._initialized = False
        self._mem_conn: Optional[sqlite3.Connection] = None

    def get_connection(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._mem_conn is None:
                conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
                conn.row_factory = sqlite3.Row
                self._mem_conn = conn
            return self._mem_conn

        ensure_directories()
        conn = sqlite3.connect(
            self.db_path,
            timeout=20.0,
            check_same_thread=False,
            isolation_level=None  # autocommit mode, transactions handled explicitly
        )
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.execute("PRAGMA busy_timeout = 20000;")
        cursor.close()
        return conn

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.get_connection()
        try:
            yield conn
        finally:
            if self.db_path != ":memory:":
                conn.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE;")
            yield cursor
            cursor.execute("COMMIT;")
        except Exception:
            cursor.execute("ROLLBACK;")
            raise
        finally:
            cursor.close()
            if self.db_path != ":memory:":
                conn.close()

    @contextmanager
    def session(self) -> Generator:
        """Context manager yielding a SQLAlchemy Session bound to this database."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import StaticPool
        from switch_dashboard.storage.engine import get_db_session

        if self.db_path == ":memory:":
            if not hasattr(self, "_engine") or self._engine is None:
                conn = self.get_connection()
                self._engine = create_engine("sqlite://", creator=lambda: conn, poolclass=StaticPool)
            sess = Session(self._engine, autoflush=False, expire_on_commit=False)
            try:
                yield sess
                sess.commit()
            except Exception:
                sess.rollback()
                raise
            finally:
                sess.close()
        else:
            url = f"sqlite:///{os.path.abspath(self.db_path)}" if self.db_path else None
            with get_db_session(url) as sess:
                yield sess

    def init_db(self):
        if self._initialized:
            return
        logger.info(f"Initializing SQLite database at {self.db_path}")
        with self.connect() as conn:
            with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
                schema_sql = f.read()
            conn.executescript(schema_sql)
            try:
                conn.execute("ALTER TABLE discovered_clients ADD COLUMN is_mini_switch INTEGER DEFAULT 0")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE discovered_clients ADD COLUMN passthrough_port TEXT DEFAULT 'PC'")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE discovered_clients ADD COLUMN custom_name TEXT DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE discovered_clients ADD COLUMN ssid TEXT DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE discovered_clients ADD COLUMN signal_dbm INTEGER")
            except Exception:
                pass

        try:
            with self.session() as s:
                from switch_dashboard.storage.models import Base
                Base.metadata.create_all(s.get_bind())
        except Exception as e:
            logger.debug(f"Metadata create_all notice: {e}")

        self._initialized = True
        logger.info("Database schema initialized successfully with WAL mode.")


_default_db: Optional[Database] = None


def get_db(db_path: Optional[str] = None) -> Database:
    global _default_db
    if db_path is not None:
        return Database(db_path)
    if _default_db is None:
        _default_db = Database()
        _default_db.init_db()
    return _default_db

