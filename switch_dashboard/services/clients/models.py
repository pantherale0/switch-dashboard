import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def clean_mac_str(mac: str) -> str:
    """Normalizes MAC addresses to uppercase colon-separated format (XX:XX:XX:XX:XX:XX)."""
    clean = re.sub(r"[^0-9A-Fa-f]", "", str(mac or "")).upper()
    if len(clean) == 12:
        return ":".join(clean[i : i + 2] for i in range(0, 12, 2))
    return str(mac or "").strip()


def format_bps(bps: int) -> str:
    """Formats numeric bits per second into human-readable network bandwidth units."""
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.1f} Gbps"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.1f} Kbps"
    return f"{bps} bps"


def format_bytes(bytes_count: int) -> str:
    """Formats raw byte counts into human-readable storage/transfer units."""
    if bytes_count >= 1_073_741_824:
        return f"{bytes_count / 1_073_741_824:.2f} GB"
    if bytes_count >= 1_048_576:
        return f"{bytes_count / 1_048_576:.1f} MB"
    if bytes_count >= 1024:
        return f"{bytes_count / 1024:.0f} KB"
    return f"{bytes_count} B"


def signal_dbm_to_percent(dbm: Optional[int]) -> int:
    """Converts Wi-Fi RSSI (in dBm) to a standardized 0-100% signal strength scale."""
    if dbm is None:
        return 0
    # Standard formula: -50 dBm = 100%, -100 dBm = 0%
    return max(0, min(100, int(2 * (dbm + 100))))


def get_subnet_cidr(ip: str) -> str:
    """Calculates standard subnet CIDR for an IPv4 address (defaults to /24)."""
    if not ip or ":" in ip:
        return "Unknown"
    try:
        obj = ipaddress.IPv4Interface(f"{ip}/24")
        return str(obj.network)
    except Exception:
        return "192.168.1.0/24"


def clean_port_name(port: Any) -> str:
    """Canonicalizes port strings to prevent spurious differences
    (e.g., 'Port 1' -> '1', 'port1' -> '1', trimming whitespace).
    """
    if port is None:
        return ""
    p = str(port).strip()
    match = re.match(r"^(?:port[\s\-_]*)(\d+)$", p, re.IGNORECASE)
    if match:
        return match.group(1)
    return p


@dataclass(slots=True)
class ClientSighting:
    """Represents a single observation of a client MAC on a specific node and port."""
    mac: str
    node_id: str
    port: str
    node_name: str = ""
    node_role: str = "switch"
    node_device_type: str = "switch"
    vlan: str = "1"
    ip: str = ""
    hostname: str = ""
    ssid: str = ""
    signal_dbm: Optional[int] = None
    rx_bytes: Optional[int] = None
    tx_bytes: Optional[int] = None
    rx_rate: Optional[float] = None
    tx_rate: Optional[float] = None
    is_child: bool = False
    native_id: str = ""
    speed_tx_bps: int = 0
    speed_rx_bps: int = 0


@dataclass(slots=True)
class ResolvedEdge:
    """Result of the central edge resolution algorithm determining leaf physical attachment."""
    mac: str
    node_id: str
    port: str
    node_name: str = ""
    vlan: str = "1"
    ssid: str = ""
    signal_dbm: Optional[int] = None
    is_child: bool = False
    score: int = 0
    ip: str = ""
    hostname: str = ""


@dataclass
class ClientLocationState:
    """In-memory tracked physical location of a client."""
    switch_ip: str
    switch_name: str
    port: str
    vlan: str = "1"
    ssid: str = ""
    signal_dbm: Optional[int] = None
    is_child: bool = False
    connected_at: float = 0.0
    last_roamed_at: float = 0.0
    footprint: Dict[str, str] = field(default_factory=dict)
    footprint_hash: str = ""


@dataclass
class LiveSpeed:
    """Real-time calculated traffic speeds and cumulative byte counters for a client."""
    speed_tx_bps: int = 0
    speed_rx_bps: int = 0
    cum_tx: int = 0
    cum_rx: int = 0
    ts: float = 0.0

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "speed_tx_bps": self.speed_tx_bps,
            "speed_rx_bps": self.speed_rx_bps,
            "cum_tx": self.cum_tx,
            "cum_rx": self.cum_rx,
            "ts": self.ts,
        }
