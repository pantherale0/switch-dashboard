#!/usr/bin/env python3
"""Switch Dashboard Application Entry Point."""

import os
from switch_dashboard.web import create_app
from switch_dashboard.config import get_config, load_config
from switch_dashboard.services.poller_service import unify_speed, format_bps
from switch_dashboard.services.topology_service import normalize_mac, is_ignored_mac
from switch_dashboard.services.vendor_service import get_vendor_service

# Legacy symbol exports for backward compatibility
VERSION = "2.0.0"
vendor_service = get_vendor_service()
load_mac_vendors = vendor_service.load_mac_vendors
get_ieee_vendors = vendor_service.get_ieee_vendors
lookup_vendor = vendor_service.lookup_vendor

app = create_app()

if __name__ == "__main__":
    cfg = get_config()
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("DASHBOARD_PORT", cfg.get("web_port", 8080)))
    app.run(host=host, port=port, debug=False)
