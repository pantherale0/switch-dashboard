from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any


@dataclass
class PortData:
    port: Any
    link: str = "down"
    speed: str = ""
    duplex: str = ""
    flow_control: str = ""
    tx_bytes: int = 0
    rx_bytes: int = 0
    tx_packets: int = 0
    rx_packets: int = 0
    cum_tx: int = 0
    cum_rx: int = 0
    speed_tx_bps: int = 0
    speed_rx_bps: int = 0
    sfp_present: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MacEntry:
    port: Any
    mac: str
    vlan: Any = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "port": str(self.port),
            "mac": self.mac,
            "vlan": str(self.vlan),
        }


@dataclass
class TransceiverInfo:
    port: Any
    vendor: str = ""
    part_number: str = ""
    serial: str = ""
    wavelength: str = ""
    temperature: str = ""
    voltage: str = ""
    tx_bias: str = ""
    tx_power: str = ""
    rx_power: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SwitchData:
    ip: str
    name: str = ""
    model: str = "Generic Model"
    mac: str = ""
    firmware: str = ""
    uptime: str = ""
    status: str = "online"
    ports: List[Dict[str, Any]] = field(default_factory=list)
    mac_table: List[Dict[str, Any]] = field(default_factory=list)
    mac_timestamp: float = 0.0
    timestamp: float = 0.0
    error: Optional[str] = None
    vm_mac_map: Dict[str, str] = field(default_factory=dict)
    role: Optional[str] = None
    neighbors: List[Dict[str, Any]] = field(default_factory=list)
    child_devices: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MetricPoint:
    ts: float
    tx: int
    rx: int

    def to_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts, "tx": self.tx, "rx": self.rx}


@dataclass
class HourlyTrendPoint:
    hour_ts: int
    sample_count: int
    avg_tx: int
    max_tx: int
    min_tx: int
    avg_rx: int
    max_rx: int
    min_rx: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class NodeRole(str):
    INTERNET = "internet"
    ONT = "ont"
    ROUTER = "router"
    DISTRIBUTION = "distribution"
    CORE = "core"
    ACCESS = "access"
    SWITCH = "switch"
    ACCESS_POINT = "access_point"
    UNMANAGED_SWITCH = "unmanaged_switch"
    PHONE = "phone"
    SERVER = "server"
    CLIENT = "client"
    VIRTUALISATION_HOST = "virtualisation_host"
    VIRTUAL_MACHINE = "virtual_machine"
    LXC_CONTAINER = "lxc_container"


class PortRole(str):
    UPLINK = "uplink"              # Egress to upstream switch/router
    DOWNLINK = "downlink"          # Ingress to downstream switch
    INTER_SWITCH = "inter_switch"  # Peer trunk link
    AP_TRUNK = "ap_trunk"          # Link to Access Point (multi-MAC wireless bridge)
    ACCESS = "access"              # Single client host
    UNMANAGED_HUB = "unmanaged"    # Multi-client edge port (dumb switch)
    ROUTED = "routed"              # L3 routed port
    UNUSED = "unused"              # Port admin down or link down


@dataclass
class PortTelemetry:
    tx_bps: int = 0
    rx_bps: int = 0
    speed_bps: int = 1_000_000_000
    utilization_pct: float = 0.0
    tx_packets: int = 0
    rx_packets: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NeighborInfo:
    local_port: str
    remote_chassis_id: str = ""
    remote_port_id: str = ""
    remote_system_name: str = ""
    protocol: str = "lldp"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EnhancedPortInfo:
    port_id: str
    name: str
    link_status: str = "down"
    speed: str = ""
    duplex: str = "Full"
    role: str = PortRole.UNUSED
    connected_node_id: Optional[str] = None
    connected_port_id: Optional[str] = None
    learned_macs: List[str] = field(default_factory=list)
    telemetry: PortTelemetry = field(default_factory=PortTelemetry)

    def to_dict(self) -> Dict[str, Any]:
        is_up = "up" in str(self.link_status).lower()
        t_dict = self.telemetry.to_dict() if hasattr(self.telemetry, "to_dict") else (self.telemetry or {})
        return {
            "port_id": str(self.port_id),
            "port": str(self.port_id),
            "name": self.name,
            "link_status": self.link_status,
            "link": self.link_status,
            "status": "up" if is_up else "down",
            "speed": self.speed,
            "duplex": self.duplex,
            "role": self.role,
            "connected_node_id": self.connected_node_id,
            "connected_port_id": self.connected_port_id,
            "learned_macs": self.learned_macs,
            "speed_tx_bps": t_dict.get("tx_bps", 0),
            "speed_rx_bps": t_dict.get("rx_bps", 0),
            "tx_packets": t_dict.get("tx_packets", 0),
            "rx_packets": t_dict.get("rx_packets", 0),
            "telemetry": t_dict,
        }


@dataclass
class NetworkLinkInfo:
    id: str
    source: str
    target: str
    source_port: str
    target_port: str
    speed: str = "1G"
    link_type: str = "uplink"
    status: str = "online"
    tx_bps: int = 0
    rx_bps: int = 0
    capacity_bps: int = 1_000_000_000
    utilization_pct: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
