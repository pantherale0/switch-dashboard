import os
import atexit
import logging
from typing import Optional, Dict, Any
from flask import Flask

from switch_dashboard.config import (
    PROJECT_ROOT,
    DATA_DIR,
    load_config,
    OUI_TXT_PATH,
    OUI36_TXT_PATH,
)
from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.repositories.metric_repo import MetricRepository
from switch_dashboard.storage.housekeeper import Housekeeper
from switch_dashboard.storage.migration import migrate_legacy_data
from switch_dashboard.services.log_service import setup_logging
from switch_dashboard.services.poller_service import get_poller_service, format_bps
from switch_dashboard.services.scanner_service import get_scanner_service
from switch_dashboard.services.vendor_service import get_vendor_service
from switch_dashboard.web.blueprints import (
    dashboard_bp,
    topology_bp,
    metrics_bp,
    config_bp,
    backups_bp,
    logs_bp,
    scanner_bp,
    protocols_bp,
    docs_bp,
    clients_bp,
)

logger = logging.getLogger("switch_dashboard.web")


def create_app(
    config_override: Optional[Dict[str, Any]] = None,
    start_background_workers: bool = True,
) -> Flask:
    """Application Factory for switch-dashboard."""
    os.umask(0o077)
    template_dir = os.path.join(PROJECT_ROOT, "templates")
    static_dir = os.path.join(PROJECT_ROOT, "static")

    app = Flask(
        "switch_dashboard",
        template_folder=template_dir,
        static_folder=static_dir,
    )
    app.testing = os.environ.get("TESTING") == "1"
    app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_CONTENT_LENGTH", str(2 * 1024 * 1024)))
    if not app.testing:
        from switch_dashboard.security.crypto import get_key
        get_key()
    from switch_dashboard import __version__
    app.version = os.environ.get("APP_VERSION", __version__)
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # Setup logging
    setup_logging()

    # Load configuration
    cfg = load_config()
    if config_override:
        cfg.update(config_override)

    # Initialize Database & Run Alembic Migrations
    try:
        from switch_dashboard.storage.migrations import run_migrations
        run_migrations()
    except Exception as e:
        logger.critical(f"Database migration failed: {e}")
        raise

    db = get_db()
    db.init_db()

    metric_repo = MetricRepository(db)
    migrate_legacy_data(metric_repo, db)

    # Template filters
    @app.template_filter("format_bps")
    def _jinja_format_bps(bps):
        try:
            return format_bps(int(bps))
        except Exception:
            return str(bps)

    @app.template_filter("format_bytes")
    def _jinja_format_bytes(b):
        try:
            val = int(b)
            if val >= 1_000_000_000_000:
                return f"{val / 1_000_000_000_000:.2f} TB"
            if val >= 1_000_000_000:
                return f"{val / 1_000_000_000:.2f} GB"
            if val >= 1_000_000:
                return f"{val / 1_000_000:.2f} MB"
            if val >= 1_000:
                return f"{val / 1_000:.2f} KB"
            return f"{val} B"
        except Exception:
            return str(b)

    # Register Blueprints
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(topology_bp)
    app.register_blueprint(metrics_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(backups_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(scanner_bp)
    app.register_blueprint(protocols_bp)
    app.register_blueprint(docs_bp)
    app.register_blueprint(clients_bp)

    from switch_dashboard.web.auth import init_auth
    init_auth(app)

    # Start background services
    import sys
    is_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules)
    if start_background_workers and not app.testing and not is_pytest:
        # Poller service
        poller = get_poller_service()
        poller.start()

        # Scanner service
        scanner = get_scanner_service()
        scanner.start()

        # Zabbix-style Housekeeper
        housekeeper = Housekeeper(db)
        housekeeper.start(interval_seconds=3600)

        # OUI background download if missing
        if not os.path.exists(OUI_TXT_PATH) or not os.path.exists(OUI36_TXT_PATH):
            vendor_service = get_vendor_service()
            import threading
            threading.Thread(target=vendor_service.download_oui_files, daemon=True).start()

        def _cleanup():
            try:
                poller.stop()
                scanner.stop()
                housekeeper.stop()
            except Exception:
                pass

        atexit.register(_cleanup)

    logger.info("Switch Dashboard application initialized successfully.")
    return app
