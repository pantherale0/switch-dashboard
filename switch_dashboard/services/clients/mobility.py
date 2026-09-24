import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from switch_dashboard.services.clients.models import (
    ClientLocationState,
    ResolvedEdge,
    clean_mac_str,
)

logger = logging.getLogger("switch_dashboard.services.clients.mobility")


class MobilityTracker:
    """Evaluates physical edge attachment transitions and manages anti-flapping debounce."""

    def __init__(self, device_repo, min_roam_debounce_seconds: float = 15.0):
        self.device_repo = device_repo
        self.min_roam_debounce_seconds = min_roam_debounce_seconds
        self._locations: Dict[str, ClientLocationState] = {}
        self._warmed = False
        self._transition_history: Dict[str, List[Tuple[str, str, float]]] = defaultdict(list)

    def warm_locations_if_needed(self):
        """Hydrates known client locations from database on cold boot to prevent restart connection spikes."""
        if self._warmed:
            return
        self._warmed = True
        try:
            clients = self.device_repo.get_all_discovered_clients()
            for c in clients.values():
                mac = clean_mac_str(c.get("mac") or "")
                sw_ip = c.get("switch_ip")
                port = c.get("port")
                if mac and sw_ip and port:
                    self._locations[mac] = ClientLocationState(
                        switch_ip=sw_ip,
                        switch_name=c.get("switch_name", "") or "",
                        port=str(port),
                        vlan=str(c.get("vlan") or "1"),
                        ssid=str(c.get("ssid", "") or ""),
                        signal_dbm=c.get("signal_dbm"),
                        is_child=False,
                        connected_at=float(c.get("last_seen") or time.time()),
                        last_roamed_at=0.0,
                    )
            logger.info(f"Hydrated {len(self._locations)} client locations from database cache.")
        except Exception as e:
            logger.warning(f"Failed to hydrate client locations from database: {e}")

    def get_location(self, mac: str) -> Optional[ClientLocationState]:
        return self._locations.get(mac)

    def set_location(self, mac: str, loc: ClientLocationState):
        self._locations[mac] = loc

    def evaluate_mobility(
        self,
        mac: str,
        edge: ResolvedEdge,
        footprint: Dict[str, str],
        footprint_hash: str,
        ts: float,
    ) -> Tuple[Optional[str], ClientLocationState]:
        """Evaluates whether a client's resolved edge attachment has changed and emits
        mobility events with anti-flapping debounce protection.

        Returns:
            Tuple of (event_type: 'connected' | 'roamed' | 'debounced' | None, ClientLocationState)
        """
        prev_loc = self._locations.get(mac)

        if prev_loc is None:
            # First time seen on the network
            self.device_repo.record_client_connection_event(
                mac=mac,
                event_type="connected",
                switch_ip=edge.node_id,
                switch_name=edge.node_name,
                port=edge.port,
                vlan=edge.vlan,
                ssid=edge.ssid,
                signal_dbm=edge.signal_dbm,
                connected_at=ts,
            )
            state = ClientLocationState(
                switch_ip=edge.node_id,
                switch_name=edge.node_name,
                port=edge.port,
                vlan=edge.vlan,
                ssid=edge.ssid,
                signal_dbm=edge.signal_dbm,
                is_child=edge.is_child,
                connected_at=ts,
                last_roamed_at=ts,
                footprint=footprint,
                footprint_hash=footprint_hash,
            )
            self._locations[mac] = state
            logger.info(
                f"Client {mac} newly connected to network at "
                f"{edge.node_name or edge.node_id}:{edge.port} "
                f"(VLAN {edge.vlan}{', SSID ' + edge.ssid if edge.ssid else ''})"
            )
            return ("connected", state)

        # Check if the physical edge location changed
        if (prev_loc.switch_ip != edge.node_id or str(prev_loc.port).lower() != str(edge.port).lower()) and (edge.node_id and edge.port):
            # Check for ping-pong / rapid port flapping within 180 seconds
            cutoff = ts - 180.0
            history = [t for t in self._transition_history[mac] if t[2] >= cutoff]
            self._transition_history[mac] = history

            # If the client recently visited this node:port (ping-pong) or has roamed >= 3 times in 180s
            is_ping_pong = any(t[0] == edge.node_id and str(t[1]).lower() == str(edge.port).lower() for t in history)
            if (len(history) >= 1 and is_ping_pong) or len(history) >= 3:
                logger.warning(
                    f"Port bouncing/flapping dampened for client {mac}: attempted jump to "
                    f"{edge.node_name or edge.node_id}:{edge.port} (already {len(history)} transitions in 180s). "
                    f"Holding stable at {prev_loc.switch_name or prev_loc.switch_ip}:{prev_loc.port}."
                )
                prev_loc.footprint = footprint
                prev_loc.footprint_hash = footprint_hash
                return ("flapping_dampened", prev_loc)

            last_roamed = prev_loc.last_roamed_at or prev_loc.connected_at
            if (ts - last_roamed) >= self.min_roam_debounce_seconds:
                self.device_repo.record_client_connection_event(
                    mac=mac,
                    event_type="roamed",
                    switch_ip=edge.node_id,
                    switch_name=edge.node_name,
                    port=edge.port,
                    vlan=edge.vlan,
                    ssid=edge.ssid,
                    signal_dbm=edge.signal_dbm,
                    from_switch_ip=prev_loc.switch_ip,
                    from_port=prev_loc.port,
                    connected_at=ts,
                )
                logger.info(
                    f"Client {mac} roamed: {prev_loc.switch_name or prev_loc.switch_ip}:{prev_loc.port} -> "
                    f"{edge.node_name or edge.node_id}:{edge.port} (VLAN {edge.vlan}, dt={ts - last_roamed:.1f}s)"
                )
                self._transition_history[mac].append((prev_loc.switch_ip, prev_loc.port, ts))

                prev_loc.switch_ip = edge.node_id
                prev_loc.switch_name = edge.node_name
                prev_loc.port = edge.port
                prev_loc.vlan = edge.vlan
                prev_loc.ssid = edge.ssid
                prev_loc.signal_dbm = edge.signal_dbm
                prev_loc.is_child = edge.is_child
                prev_loc.last_roamed_at = ts
                prev_loc.footprint = footprint
                prev_loc.footprint_hash = footprint_hash
                return ("roamed", prev_loc)
            else:
                logger.debug(
                    f"Debounced rapid roam for {mac}: {prev_loc.switch_ip}:{prev_loc.port} -> "
                    f"{edge.node_id}:{edge.port} (dt={ts - last_roamed:.1f}s < {self.min_roam_debounce_seconds}s)"
                )
                return ("debounced", prev_loc)

        # Edge location remains identical
        prev_loc.footprint = footprint
        prev_loc.footprint_hash = footprint_hash
        return (None, prev_loc)
