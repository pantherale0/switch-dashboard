from switch_dashboard.web.blueprints.dashboard import dashboard_bp
from switch_dashboard.web.blueprints.topology import topology_bp
from switch_dashboard.web.blueprints.metrics import metrics_bp
from switch_dashboard.web.blueprints.config import config_bp
from switch_dashboard.web.blueprints.backups import backups_bp
from switch_dashboard.web.blueprints.logs import logs_bp
from switch_dashboard.web.blueprints.scanner import scanner_bp
from switch_dashboard.web.blueprints.protocols import protocols_bp
from switch_dashboard.web.blueprints.docs import docs_bp
from switch_dashboard.web.blueprints.clients import clients_bp

__all__ = [
    "dashboard_bp",
    "topology_bp",
    "metrics_bp",
    "config_bp",
    "backups_bp",
    "logs_bp",
    "scanner_bp",
    "protocols_bp",
    "docs_bp",
    "clients_bp",
]

