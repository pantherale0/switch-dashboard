"""Switch Scraper Facade (Backwards Compatibility).

This module re-exports scraper protocol drivers from switch_dashboard.protocols
to maintain full compatibility with existing external tools and tests.
"""

from switch_dashboard.protocols import (
    HCSwitchProtocol as HCSwitchScraper,
    OVSProtocol as OVSScraper,
    FritzBoxProtocol as FritzBoxScraper,
    SSHProtocol as SSHScraper,
    UnifiProtocol as UnifiScraper,
    ProxmoxProtocol as ProxmoxScraper,
    get_switch_lock,
    scrape_switch,
)

__all__ = [
    "HCSwitchScraper",
    "OVSScraper",
    "FritzBoxScraper",
    "SSHScraper",
    "UnifiScraper",
    "ProxmoxScraper",
    "get_switch_lock",
    "scrape_switch",
]
