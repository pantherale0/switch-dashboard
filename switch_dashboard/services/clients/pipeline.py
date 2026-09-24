import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from switch_dashboard.services.clients.classifier import DeviceClassifier
from switch_dashboard.services.clients.hasher import compute_footprint_hash
from switch_dashboard.services.clients.metrics import ClientMetricsCalculator
from switch_dashboard.services.clients.mobility import MobilityTracker
from switch_dashboard.services.clients.models import (
    ClientSighting,
    ResolvedEdge,
    clean_mac_str,
    clean_port_name,
)
from switch_dashboard.services.clients.resolver import EdgeResolver

logger = logging.getLogger("switch_dashboard.services.clients.pipeline")


class ClientHandler:
    """Post-polling client ingestion and resolution pipeline.

    Processes raw multi-node telemetry, performs whole-network edge resolution,
    tracks mobility with deterministic footprint hashing, and computes bandwidth metrics.
    """

    def __init__(
        self,
        device_repo,
        vendor_service,
        mobility_tracker: Optional[MobilityTracker] = None,
        metrics_calculator: Optional[ClientMetricsCalculator] = None,
        edge_resolver: Optional[EdgeResolver] = None,
        classifier: Optional[DeviceClassifier] = None,
    ):
        self.device_repo = device_repo
        self.vendor_service = vendor_service
        self.mobility = mobility_tracker or MobilityTracker(device_repo)
        self.metrics = metrics_calculator or ClientMetricsCalculator()
        self.resolver = edge_resolver or EdgeResolver()
        self.classifier = classifier or DeviceClassifier()
        self._last_footprint_hashes: Dict[str, str] = {}
        self._last_seen_times: Dict[str, float] = {}

    def process_cycle(
        self,
        results: Dict[str, Dict[str, Any]],
        now: Optional[float] = None,
    ):
        """Executes the post-poll ingestion pipeline on freshly scraped node telemetry."""
        ts = now or time.time()
        logger.debug(f"Starting client processing cycle for {len(results)} reported devices")
        self.mobility.warm_locations_if_needed()

        # Step 1: Pre-compute per-node port MAC sets and interlink/uplink ports
        port_mac_sets: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
        interlinks: Dict[str, Set[str]] = defaultdict(set)
        online_nodes: Dict[str, Dict[str, Any]] = {}

        for sw_ip, data in results.items():
            if not isinstance(data, dict):
                continue
            if data.get("status", "online") != "online":
                continue
            node_id = str(data.get("id") or sw_ip)
            online_nodes[node_id] = data

            # Collect known uplink port
            uplink_port = clean_port_name(data.get("uplink_port"))
            if uplink_port:
                interlinks[node_id].add(uplink_port)

            # Collect LLDP/CDP neighbor inter-switch links
            for n in data.get("neighbors") or []:
                if isinstance(n, dict):
                    p = clean_port_name(n.get("port") or n.get("local_port"))
                    if p:
                        interlinks[node_id].add(p)

            # Pre-populate port MAC sets for density calculation
            for entry in data.get("mac_table") or []:
                if not isinstance(entry, dict):
                    continue
                mac = clean_mac_str(entry.get("mac") or "")
                if not mac or mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                    continue
                port = clean_port_name(entry.get("port"))
                if port:
                    port_mac_sets[node_id][port].add(mac)

        # Step 2: Accumulate candidate sightings and build {node_id: port} footprints
        sightings_by_mac: Dict[str, List[ClientSighting]] = defaultdict(list)
        footprints_by_mac: Dict[str, Dict[str, str]] = defaultdict(dict)

        for node_id, data in online_nodes.items():
            node_name = data.get("name") or node_id
            node_role = str(data.get("role") or data.get("device_type") or "switch").lower()
            node_device_type = str(data.get("device_type") or data.get("role") or "switch").lower()

            # Physical MAC table entries
            for entry in data.get("mac_table") or []:
                if not isinstance(entry, dict):
                    continue
                mac = clean_mac_str(entry.get("mac") or "")
                if not mac or mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                    continue

                port = clean_port_name(entry.get("port"))
                vlan = str(entry.get("vlan") or "1").strip()
                ip = str(entry.get("ip") or "").strip()
                hostname = str(entry.get("hostname") or "").strip()
                ssid = str(entry.get("ssid") or "").strip()
                signal_raw = entry.get("signal") or entry.get("rssi")
                signal_dbm = None
                if signal_raw is not None:
                    try:
                        signal_dbm = int(float(signal_raw))
                    except (ValueError, TypeError):
                        signal_dbm = None

                sighting = ClientSighting(
                    mac=mac,
                    node_id=node_id,
                    node_name=node_name,
                    node_role=node_role,
                    node_device_type=node_device_type,
                    port=port,
                    vlan=vlan,
                    ip=ip,
                    hostname=hostname,
                    ssid=ssid,
                    signal_dbm=signal_dbm,
                    rx_bytes=entry.get("rx_bytes"),
                    tx_bytes=entry.get("tx_bytes"),
                    rx_rate=entry.get("rx_rate"),
                    tx_rate=entry.get("tx_rate"),
                    is_child=False,
                )
                sightings_by_mac[mac].append(sighting)
                if port:
                    footprints_by_mac[mac][node_id] = port

            # Child devices (Proxmox VMs / LXCs)
            for child in data.get("child_devices") or []:
                if not isinstance(child, dict):
                    continue
                c_mac = clean_mac_str(child.get("mac") or "")
                if not c_mac:
                    continue
                c_name = child.get("name") or ""
                c_ip = child.get("ip") or ""
                native_id = str(child.get("native_id") or "")
                c_port = f"vhost:{native_id}" if native_id else "vhost"
                c_tx = child.get("speed_tx_bps", 0)
                c_rx = child.get("speed_rx_bps", 0)

                child_sighting = ClientSighting(
                    mac=c_mac,
                    node_id=node_id,
                    node_name=node_name,
                    node_role=node_role,
                    node_device_type="virtualisation_host",
                    port=c_port,
                    vlan="1",
                    ip=c_ip,
                    hostname=c_name,
                    is_child=True,
                    native_id=native_id,
                    speed_tx_bps=c_tx,
                    speed_rx_bps=c_rx,
                )
                sightings_by_mac[c_mac].append(child_sighting)
                footprints_by_mac[c_mac][node_id] = c_port

        # Step 3: Process clients via EdgeResolver & MobilityTracker
        total_sightings = sum(len(s) for s in sightings_by_mac.values())
        logger.debug(
            f"Aggregated {total_sightings} sightings across {len(sightings_by_mac)} unique MACs "
            f"from {len(online_nodes)} online nodes"
        )

        discovered_batch: List[Dict[str, Any]] = []
        samples_batch: List[Dict[str, Any]] = []

        for mac, sightings in sightings_by_mac.items():
            if not sightings:
                continue

            self._last_seen_times[mac] = ts
            footprint = footprints_by_mac.get(mac, {})
            footprint_hash = compute_footprint_hash(footprint)
            prev_hash = self._last_footprint_hashes.get(mac)
            prev_loc = self.mobility.get_location(mac)

            footprint_changed = (prev_hash is None or prev_hash != footprint_hash or prev_loc is None)

            # Metadata aggregation across sightings in this cycle
            best_ip = next((s.ip for s in sightings if s.ip), "")
            best_hostname = next((s.hostname for s in sightings if s.hostname), "")

            if not footprint_changed and prev_loc is not None:
                # Footprint is unchanged: device definitely did not move across the network
                edge_node = prev_loc.switch_ip
                edge_name = prev_loc.switch_name
                edge_port = prev_loc.port
                edge_vlan = prev_loc.vlan
                edge_ssid = prev_loc.ssid
                edge_signal = prev_loc.signal_dbm
                is_child = prev_loc.is_child
            else:
                # Footprint changed or initial discovery: central edge resolution
                if prev_hash is not None and prev_hash != footprint_hash:
                    logger.debug(
                        f"Footprint changed for client {mac}: {prev_hash[:8]} -> {footprint_hash[:8]}, resolving edge..."
                    )
                incumbent = (prev_loc.switch_ip, prev_loc.port) if prev_loc else None
                resolved = self.resolver.resolve_edge(
                    mac=mac,
                    sightings=sightings,
                    port_mac_sets=port_mac_sets,
                    interlinks=interlinks,
                    incumbent_location=incumbent,
                )
                edge_node = resolved.node_id
                edge_name = resolved.node_name
                edge_port = resolved.port
                edge_vlan = resolved.vlan
                edge_ssid = resolved.ssid
                edge_signal = resolved.signal_dbm
                is_child = resolved.is_child

                # Evaluate mobility & debounce
                self.mobility.evaluate_mobility(mac, resolved, footprint, footprint_hash, ts)
                self._last_footprint_hashes[mac] = footprint_hash

            # Record IP observation
            if best_ip:
                self.device_repo.record_client_ip_observation(mac, best_ip, best_hostname, ts)

            # Bandwidth & bitrates
            speed_tx, speed_rx, sample = self.metrics.compute_bandwidth(mac, sightings, is_child, ts)
            if sample:
                samples_batch.append(sample)

            # Device classification & vendor
            vendor = (
                "QEMU / Proxmox Virtual Machine"
                if is_child
                else self.vendor_service.lookup_vendor(mac)
            )

            discovered_batch.append({
                "mac": mac,
                "ip": best_ip,
                "hostname": best_hostname,
                "vendor": vendor,
                "device_type": "server" if is_child else "",
                "switch_ip": edge_node,
                "port": edge_port,
                "vlan": edge_vlan,
                "ssid": edge_ssid,
                "signal_dbm": edge_signal,
                "status": "online",
                "last_seen_time": ts,
            })

        # Step 4: Batch database storage
        if discovered_batch:
            try:
                self.device_repo.upsert_discovered_clients_batch(discovered_batch)
                logger.debug(f"Persisted batch of {len(discovered_batch)} discovered clients to DB")
            except Exception as e:
                logger.error(f"Error persisting discovered clients batch: {e}")

        if samples_batch:
            try:
                self.device_repo.record_client_metric_samples_batch(samples_batch)
                logger.debug(f"Persisted batch of {len(samples_batch)} client metric samples to DB")
            except Exception as e:
                logger.error(f"Error persisting client metric samples batch: {e}")

        logger.info(
            f"Client processing cycle finished in {time.time() - ts:.3f}s: "
            f"{len(discovered_batch)} clients updated, {len(samples_batch)} metric samples recorded."
        )
