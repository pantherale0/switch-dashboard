from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, Any, List, Optional, Set, Tuple
import voluptuous as vol


def voluptuous_to_ui_schema(schema: Optional[vol.Schema]) -> List[Dict[str, Any]]:
    """Converts a Voluptuous Schema into a list of field descriptors for UI forms."""
    if not schema or not hasattr(schema, "schema") or not isinstance(schema.schema, dict):
        return []

    fields = []
    for k, v in schema.schema.items():
        name = k.schema if hasattr(k, "schema") else str(k)
        required = isinstance(k, vol.Required)
        default = k.default() if callable(getattr(k, "default", None)) else getattr(k, "default", None)
        if default is vol.UNDEFINED:
            default = None
        description = getattr(k, "description", "") or ""

        field_type = "text"
        choices = None

        if isinstance(v, vol.In):
            field_type = "select"
            choices = list(v.container)
        elif v is int or (hasattr(v, "type") and v.type is int):
            field_type = "number"
        elif v is bool or (hasattr(v, "type") and v.type is bool):
            field_type = "boolean"
        elif name.lower() != "token_id" and any(
            secret in name.lower() for secret in ["password", "secret", "token", "key"]
        ):
            field_type = "password"

        fields.append({
            "name": name,
            "title": name.replace("_", " ").title(),
            "type": field_type,
            "required": required,
            "default": default,
            "description": description,
            "choices": choices,
        })
    return fields


class Capability(str, Enum):
    METRICS = "metrics"
    MAC_TABLE = "mac_table"
    TRANSCEIVERS = "transceivers"
    DHCP_SNOOPING = "dhcp_snooping"
    IGMP = "igmp"
    JUMBO_FRAME = "jumbo_frame"
    BACKUP = "backup"
    REBOOT = "reboot"
    NEIGHBORS = "neighbors"
    CHILD_DEVICES = "child_devices"


class BaseProtocol(ABC):
    """Abstract base class for all switch communication protocols."""

    name: str = "base"
    display_name: str = "Base Protocol"
    capabilities: Set[Capability] = {Capability.METRICS}
    config_schema: Optional[vol.Schema] = None

    @classmethod
    def get_config_schema(cls) -> Optional[vol.Schema]:
        return cls.config_schema

    @classmethod
    def get_ui_schema(cls) -> List[Dict[str, Any]]:
        return voluptuous_to_ui_schema(cls.get_config_schema())

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.name_tag = config.get("name", config.get("ip", "Unknown"))
        self.ip = config["ip"]
        self.model = config.get("model", "Generic Model")
        self.port_count = config.get("port_count", 8)
        self.max_retries = config.get("max_retries", 5)

    @property
    def protocol_name(self) -> str:
        return getattr(type(self), "name", "base")

    def has_capability(self, cap: Capability) -> bool:
        return cap in self.capabilities

    @abstractmethod
    def test_connection(self) -> Tuple[bool, str]:
        """Tests device reachability and credentials."""
        pass

    @abstractmethod
    def scrape(self) -> Dict[str, Any]:
        """Polls switch information and port telemetry."""
        pass

    @abstractmethod
    def scrape_mac_table(self) -> List[Dict[str, Any]]:
        """Retrieves learned MAC forwarding table entries."""
        pass

    def scrape_transceiver(self) -> Optional[List[Dict[str, Any]]]:
        """Retrieves optical transceiver telemetry if supported."""
        return None

    def scrape_dhcp_snooping(self) -> Dict[str, Any]:
        """Retrieves DHCP snooping status if supported."""
        return {"enabled": False, "ports": {}}

    def scrape_igmp(self) -> Dict[str, Any]:
        """Retrieves IGMP snooping status if supported."""
        return {"enabled": False, "entries": []}

    def scrape_jumbo_frame(self) -> Dict[str, Any]:
        """Retrieves jumbo frame status if supported."""
        return {"enabled": False, "size": "Disabled"}

    def download_backup(self) -> bytes:
        """Downloads configuration backup binary/text."""
        return b""

    def reboot_switch(self) -> Optional[bool]:
        """Triggers device reboot."""
        return None

    def scrape_neighbors(self) -> List[Dict[str, Any]]:
        """Retrieves LLDP/CDP neighbor discovery table entries."""
        return []

    def scrape_child_devices(self) -> List[Dict[str, Any]]:
        """Retrieves normalized child devices owned by this device."""
        return []
