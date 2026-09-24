import re
from typing import Any, Optional


def normalize_port(port: Any) -> str:
    """Normalizes any switch port representation into a canonical numeric identifier.

    1 = Port 1, 2 = Port 2, etc.

    Examples:
        normalize_port(1) -> "1"
        normalize_port("1") -> "1"
        normalize_port("08") -> "8"
        normalize_port("Port 1") -> "1"
        normalize_port("port 1") -> "1"
        normalize_port("port-1") -> "1"
        normalize_port("P1") -> "1"
        normalize_port("LAN 1") -> "1"
        normalize_port("lan 2") -> "2"
        normalize_port("WAN") -> "WAN"
        normalize_port("eth0") -> "eth0"
    """
    if port is None:
        return ""
    if isinstance(port, int):
        return str(port)

    s = str(port).strip()
    if not s:
        return ""

    # Pure number: "1", "08", "24"
    if s.isdigit():
        return str(int(s))

    # Numbered ports with common prefixes: "Port 1", "port 1", "port-1", "port_1", "PORT:1", "p1", "LAN 1", "lan-2", "SFP 9", etc.
    m_num = re.match(r"^(?:port|p|lan|sfp\+?|eth|gi|fa)[\s\-_:.]*(\d+)$", s, re.IGNORECASE)
    if m_num:
        return str(int(m_num.group(1)))

    # Trailing SFP or interface slash style, e.g. "9/SFP" -> "9"
    m_slash = re.match(r"^(\d+)\s*/\s*(?:sfp\+?|lan|port)$", s, re.IGNORECASE)
    if m_slash:
        return str(int(m_slash.group(1)))

    # WAN port with number: "WAN 1" -> "1"
    m_wan = re.match(r"^wan[\s\-_:.]*(\d+)$", s, re.IGNORECASE)
    if m_wan:
        return str(int(m_wan.group(1)))

    # If bare "wan" (case-insensitive), normalize to uppercase
    if s.upper() in ("WAN", "WLAN"):
        return s.upper()

    return s


def ports_equal(p1: Any, p2: Any) -> bool:
    """Returns True if two port identifiers refer to the same physical port."""
    n1 = normalize_port(p1).lower()
    n2 = normalize_port(p2).lower()
    if not n1 and not n2:
        return True
    if not n1 or not n2:
        return False
    return n1 == n2


def format_port_display(port: Any) -> str:
    """Formats a port identifier into a consistent, human-friendly display label (1 = Port 1, 2 = Port 2).

    Examples:
        format_port_display(1) -> "Port 1"
        format_port_display("1") -> "Port 1"
        format_port_display("Port 1") -> "Port 1"
        format_port_display("LAN 2") -> "Port 2"
        format_port_display("WAN") -> "WAN"
        format_port_display("vlan03") -> "VLAN 3"
        format_port_display("") -> ""
    """
    norm = normalize_port(port)
    if not norm:
        return ""
    if norm.isdigit():
        return f"Port {norm}"
    if norm.lower().startswith("vlan"):
        v_num = norm[4:].lstrip("0") or "0"
        return f"VLAN {v_num}"
    return norm


def format_port_short(port: Any) -> str:
    """Formats a port identifier into a concise badge label.

    Examples:
        format_port_short(1) -> "P1"
        format_port_short("Port 1") -> "P1"
        format_port_short("LAN 2") -> "P2"
        format_port_short("WAN") -> "WAN"
        format_port_short("vlan03") -> "V3"
    """
    norm = normalize_port(port)
    if not norm:
        return ""
    if norm.isdigit():
        return f"P{norm}"
    if norm.lower().startswith("vlan"):
        v_num = norm[4:].lstrip("0") or "0"
        return f"V{v_num}"
    return norm


def port_key(ip: str, port: Any) -> str:
    """Generates a canonical key for dictionaries, metrics, and notes: '{ip}:{normalized_port}'."""
    return f"{ip}:{normalize_port(port)}"
