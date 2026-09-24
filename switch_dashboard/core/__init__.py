"""Core domain models and interfaces for switch-dashboard."""

from switch_dashboard.core.models import (
    PortData,
    SwitchData,
    MacEntry,
    TransceiverInfo,
    MetricPoint,
    HourlyTrendPoint,
    NodeRole,
    PortRole,
    PortTelemetry,
    EnhancedPortInfo,
    NetworkLinkInfo,
    NeighborInfo,
)
from switch_dashboard.core.ports import (
    normalize_port,
    ports_equal,
    format_port_display,
    format_port_short,
    port_key,
)

__all__ = [
    "PortData",
    "SwitchData",
    "MacEntry",
    "TransceiverInfo",
    "MetricPoint",
    "HourlyTrendPoint",
    "NodeRole",
    "PortRole",
    "PortTelemetry",
    "EnhancedPortInfo",
    "NetworkLinkInfo",
    "NeighborInfo",
    "normalize_port",
    "ports_equal",
    "format_port_display",
    "format_port_short",
    "port_key",
]
