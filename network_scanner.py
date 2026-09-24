"""Network Scanner Facade (Backwards Compatibility)."""

from switch_dashboard.services.scanner_service import (
    parse_port_range,
    scan_port,
    scan_ports_threaded,
    scan_network,
    is_host_reachable_by_ping,
    scapy_available,
)
from switch_dashboard.services.vendor_service import get_vendor_service


def get_vendor(mac_address: str) -> str:
    vendor_service = get_vendor_service()
    vendor = vendor_service.lookup_vendor(mac_address)
    return vendor if vendor else "Unknown"


__all__ = [
    "parse_port_range",
    "scan_port",
    "scan_ports_threaded",
    "scan_network",
    "is_host_reachable_by_ping",
    "get_vendor",
    "scapy_available",
]
