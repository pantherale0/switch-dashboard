import logging
import os
from typing import Optional

from alembic import command
from alembic.config import Config
from switch_dashboard.storage.engine import get_database_url

logger = logging.getLogger("switch_dashboard.storage.migrations")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ALEMBIC_INI_PATH = os.path.join(PROJECT_ROOT, "alembic.ini")


def get_alembic_config(db_url: Optional[str] = None) -> Config:
    """Returns an Alembic Config object configured for the current project root

    and database URL.
    """
    alembic_cfg = Config(ALEMBIC_INI_PATH)
    active_url = db_url or get_database_url()
    alembic_cfg.set_main_option("sqlalchemy.url", active_url)
    alembic_cfg.attributes["configure_logger"] = False
    return alembic_cfg


def run_migrations(db_url: Optional[str] = None) -> bool:
    """Executes all pending Alembic migrations up to head on the active database."""
    url = db_url or get_database_url()
    logger.info(f"Running database migrations for {url.split('@')[-1] if '@' in url else url}...")
    try:
        cfg = get_alembic_config(url)
        command.upgrade(cfg, "head")
        logger.info("Database migrations completed successfully.")
        return True
    except Exception as e:
        logger.error(f"Error executing database migrations: {e}", exc_info=True)
        raise
