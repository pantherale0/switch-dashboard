"""Clients package providing active client monitoring, footprint hashing,
edge resolution, mobility/roaming tracking, and metrics calculation.
"""

from switch_dashboard.services.clients.classifier import (
    DeviceClassifier,
    clean_brand_name,
)
from switch_dashboard.services.clients.hasher import compute_footprint_hash
from switch_dashboard.services.clients.metrics import ClientMetricsCalculator
from switch_dashboard.services.clients.mobility import MobilityTracker
from switch_dashboard.services.clients.models import (
    ClientLocationState,
    ClientSighting,
    LiveSpeed,
    ResolvedEdge,
    clean_mac_str,
    clean_port_name,
    format_bps,
    format_bytes,
    get_subnet_cidr,
    signal_dbm_to_percent,
)
from switch_dashboard.services.clients.pipeline import ClientHandler
from switch_dashboard.services.clients.resolver import EdgeResolver
from switch_dashboard.services.clients.service import (
    ClientMonitorService,
    get_client_monitor_service,
)

__all__ = [
    "ClientMonitorService",
    "get_client_monitor_service",
    "ClientHandler",
    "EdgeResolver",
    "compute_footprint_hash",
    "DeviceClassifier",
    "clean_brand_name",
    "MobilityTracker",
    "ClientMetricsCalculator",
    "clean_mac_str",
    "clean_port_name",
    "signal_dbm_to_percent",
    "format_bps",
    "format_bytes",
    "get_subnet_cidr",
    "ClientLocationState",
    "ClientSighting",
    "LiveSpeed",
    "ResolvedEdge",
]
