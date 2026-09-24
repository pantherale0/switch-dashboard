import logging
from typing import Any, Dict, List, Optional, Set, Tuple
from switch_dashboard.services.clients.models import ClientSighting, ResolvedEdge

logger = logging.getLogger("switch_dashboard.services.clients.resolver")


class EdgeResolver:
    """Central algorithm that determines the true physical leaf edge attachment point
    for a client device across all sightings in the network topology.
    """

    @staticmethod
    def score_sighting(
        sighting: ClientSighting,
        port_mac_sets: Dict[str, Dict[str, Set[str]]],
        interlinks: Dict[str, Set[str]],
        incumbent_location: Optional[Tuple[str, str]] = None,
    ) -> int:
        """Calculates a physical leaf edge proximity score for a candidate sighting."""
        # 1. Hypervisor VM guests are directly hosted on the virtualization node
        if sighting.is_child:
            return 10000

        node_id = sighting.node_id
        port = sighting.port
        role = sighting.node_role
        device_type = sighting.node_device_type
        score = 0

        # 2. Access Point direct wireless association
        is_ap = (
            device_type == "access_point"
            or role == "access_point"
            or bool(sighting.ssid)
            or sighting.signal_dbm is not None
        )
        if is_ap:
            score += 1000
            if sighting.ssid:
                score += 200
            if sighting.signal_dbm is not None:
                score += 100

        # 3. Port MAC Density (fewer MACs on a port = leaf access port)
        mac_count = len(port_mac_sets.get(node_id, {}).get(port, set()))
        if mac_count == 1:
            score += 500
        elif 2 <= mac_count <= 3:
            score += 300
        elif mac_count > 3:
            score += max(0, 100 - mac_count)

        # 4. Inter-switch link / uplink penalty
        if port in interlinks.get(node_id, set()):
            score -= 500

        # 5. Router / Default Gateway penalty (switches are physically closer than gateways)
        if role == "router" or device_type == "router":
            port_lower = str(port).lower()
            if any(v in port_lower for v in ("vlan", "br", "lo", "wg", "tun", "tap", "wan", "bond")):
                score -= 1000
            else:
                score -= 300

        # 6. Incumbent Attachment Stability Bonus (prevents ghost/transient port bouncing)
        if incumbent_location:
            inc_node, inc_port = incumbent_location
            if node_id == inc_node and str(port).lower() == str(inc_port).lower():
                score += 350

        return score

    @classmethod
    def resolve_edge(
        cls,
        mac: str,
        sightings: List[ClientSighting],
        port_mac_sets: Dict[str, Dict[str, Set[str]]],
        interlinks: Dict[str, Set[str]],
        incumbent_location: Optional[Tuple[str, str]] = None,
    ) -> ResolvedEdge:
        """Selects the highest scoring physical leaf edge sighting among all candidate sightings."""
        if not sightings:
            raise ValueError(f"Cannot resolve edge for {mac}: no sightings provided")

        scored_candidates = [
            (cls.score_sighting(s, port_mac_sets, interlinks, incumbent_location=incumbent_location), s)
            for s in sightings
        ]
        # Sort by score descending
        scored_candidates.sort(key=lambda pair: pair[0], reverse=True)
        best_score, winner = scored_candidates[0]

        if len(sightings) > 1:
            candidates_summary = ", ".join(
                f"{s.node_id}:{s.port} (score={sc})" for sc, s in scored_candidates
            )
            logger.debug(
                f"Resolved edge for {mac} among {len(sightings)} candidate sightings: "
                f"winner={winner.node_name or winner.node_id}:{winner.port} (score={best_score}). "
                f"Candidates: [{candidates_summary}]"
            )
        else:
            logger.debug(
                f"Resolved single sighting for {mac} at {winner.node_name or winner.node_id}:{winner.port} (score={best_score})"
            )

        best_ip = next((s.ip for s in sightings if s.ip), "")
        best_hostname = next((s.hostname for s in sightings if s.hostname), "")
        best_ssid = next((s.ssid for s in sightings if s.ssid), winner.ssid)
        best_signal = next((s.signal_dbm for s in sightings if s.signal_dbm is not None), winner.signal_dbm)

        return ResolvedEdge(
            mac=mac,
            node_id=winner.node_id,
            node_name=winner.node_name,
            port=winner.port,
            vlan=winner.vlan,
            ssid=best_ssid,
            signal_dbm=best_signal,
            is_child=winner.is_child,
            score=best_score,
            ip=best_ip,
            hostname=best_hostname,
        )
