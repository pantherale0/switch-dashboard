from switch_dashboard.protocols.base import BaseProtocol, Capability
from switch_dashboard.protocols.registry import ProtocolRegistry, register_protocol
from switch_dashboard.protocols.drivers.http_hc import HCSwitchProtocol, HCSwitchScraper, get_switch_lock
from switch_dashboard.protocols.drivers.ovs import OVSProtocol, OVSScraper
from switch_dashboard.protocols.drivers.fritzbox import FritzBoxProtocol, FritzBoxScraper
from switch_dashboard.protocols.drivers.snmp import SNMPProtocol
from switch_dashboard.protocols.drivers.ssh import SSHProtocol
from switch_dashboard.protocols.drivers.unifi import UnifiProtocol
from switch_dashboard.protocols.drivers.proxmox import ProxmoxProtocol


def scrape_switch(config: dict) -> dict:
    """Helper function to instantiate protocol and scrape switch."""
    protocol = ProtocolRegistry.create(config)
    return protocol.scrape()


__all__ = [
    "BaseProtocol",
    "Capability",
    "ProtocolRegistry",
    "register_protocol",
    "HCSwitchProtocol",
    "HCSwitchScraper",
    "OVSProtocol",
    "OVSScraper",
    "FritzBoxProtocol",
    "FritzBoxScraper",
    "SNMPProtocol",
    "SSHProtocol",
    "UnifiProtocol",
    "ProxmoxProtocol",
    "get_switch_lock",
    "scrape_switch",
]
