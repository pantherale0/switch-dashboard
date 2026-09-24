import re
import logging
from typing import Dict, Any, List, Optional, Tuple, Set

from switch_dashboard.config import get_config
from switch_dashboard.services.vendor_service import get_vendor_service
from switch_dashboard.services.host_discovery_service import get_host_discovery_service
from switch_dashboard.core.models import NodeRole, PortRole, PortTelemetry, EnhancedPortInfo, NetworkLinkInfo
from switch_dashboard.core.ports import (
    normalize_port,
    ports_equal,
    format_port_display,
    format_port_short,
    port_key,
)

logger = logging.getLogger("switch_dashboard.services.topology")


def normalize_mac(mac: str) -> str:
    if not mac:
        return ""
    return mac.replace(":", "").replace("-", "").replace(" ", "").upper()


def _canonical_node_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text.split(".", 1)[0]


def is_ignored_mac(mac: str, config: Optional[Dict[str, Any]] = None) -> bool:
    if not mac:
        return False
    mac_clean = normalize_mac(mac)
    cfg = config or get_config()
    settings = cfg.get("settings", {})
    ignored_patterns = settings.get("ignored_macs", [])
    if isinstance(ignored_patterns, str):
        ignored_patterns = [p.strip() for p in ignored_patterns.split(",") if p.strip()]
    for pattern in ignored_patterns:
        pattern_clean = pattern.strip().upper()
        if not pattern_clean:
            continue
        if pattern_clean.endswith("*"):
            prefix = pattern_clean[:-1].replace(":", "").replace("-", "")
            if mac_clean.startswith(prefix):
                return True
        else:
            full_pattern = pattern_clean.replace(":", "").replace("-", "")
            if mac_clean == full_pattern:
                return True
    return False


def parse_speed_bps(speed_str: str) -> int:
    """Parses speed strings (e.g. 10G, 2.5G, 1000M, 100M, 1G/300M) into capacity bps."""
    if not speed_str:
        return 1_000_000_000
    s = str(speed_str).strip().upper()
    if "/" in s:
        s = s.split("/")[0].strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)[B]?[P]?[S]?$", s)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        if unit == "T":
            return int(val * 1_000_000_000_000)
        elif unit == "G":
            return int(val * 1_000_000_000)
        elif unit == "M":
            return int(val * 1_000_000)
        elif unit == "K":
            return int(val * 1_000)
        else:
            if val <= 100:
                return int(val * 1_000_000_000)
            elif val <= 10000:
                return int(val * 1_000_000)
            return int(val)
    return 1_000_000_000


class TopologyService:
    def __init__(self, poller_service=None, vendor_service=None, device_repo=None):
        from switch_dashboard.services.poller_service import get_poller_service
        from switch_dashboard.storage.repositories.device_repo import DeviceRepository
        self.poller_service = poller_service or get_poller_service()
        self.vendor_service = vendor_service or get_vendor_service()
        self.device_repo = device_repo or DeviceRepository()
        self._physical_host_link_cache: Dict[str, Tuple[str, str]] = {}

    def build_topology(self) -> Dict[str, Any]:
        config = get_config()
        switch_configs = config.get("switches", [])
        access_points_config = config.get("access_points", [])
        infra_devices = config.get("infrastructure_devices", [])
        unmanaged_switches = config.get("unmanaged_switches", [])
        db_clients = config.get("clients", {})
        try:
            discovered_db = self.device_repo.get_all_discovered_clients()
        except Exception as e:
            logger.warning(f"Error loading discovered clients from database: {e}")
            discovered_db = {}

        vendors = self.vendor_service.get_custom_vendors()
        ieee_vendors = self.vendor_service.get_ieee_vendors()
        lookup_vendor = self.vendor_service.lookup_vendor

        sw_data_copy = self.poller_service.get_cached_data()

        # -------------------------------------------------------------
        # 1. Ingest Managed Switches & Routers
        # -------------------------------------------------------------
        switches_by_ip: Dict[str, Dict[str, Any]] = {}
        mac_to_switch_ip: Dict[str, str] = {}
        router_mac: Optional[str] = None
        root_ip: Optional[str] = None

        for sw in switch_configs:
            if not sw.get("enabled", True):
                continue
            ip = sw["ip"]
            sw_data = sw_data_copy.get(ip, {})
            sw_mac = normalize_mac(sw_data.get("mac", "") or sw.get("mac", ""))
            
            configured_role = sw.get("role")
            configured_protocol = str(sw.get("protocol", sw_data.get("protocol", ""))).lower()
            model_lower = str(sw.get("model", "")).lower()
            name_lower = str(sw.get("name", "")).lower()

            if configured_protocol == "proxmox":
                role = NodeRole.VIRTUALISATION_HOST
            elif configured_role:
                role = configured_role
            elif model_lower == "fritzbox" or "router" in name_lower or "gateway" in name_lower or "firewall" in name_lower:
                role = NodeRole.ROUTER
            elif "dist" in name_lower or "dist" in model_lower:
                role = NodeRole.DISTRIBUTION
            elif "core" in name_lower or "core" in model_lower:
                role = NodeRole.CORE
            elif "ap" in name_lower or "access point" in name_lower or model_lower == "ap":
                role = NodeRole.ACCESS_POINT
            else:
                role = NodeRole.SWITCH

            mac_table = sw_data.get("mac_table")
            if not mac_table:
                mac_table = self.device_repo.get_mac_table(ip)
            ports = sw_data.get("ports", [])

            sw_entry = {
                "ip": ip,
                "name": sw["name"],
                "model": sw.get("model", "Generic Switch"),
                "mac": sw_mac,
                "role": role,
                "status": sw_data.get("status") or ("online" if "error" not in sw_data and sw_data else ("online" if mac_table else "offline")),
                "ports": ports,
                "mac_table": mac_table or [],
                "neighbors": sw_data.get("neighbors", []),
                "device_type": sw.get("device_type", role),
                "protocol": sw.get("protocol", sw_data.get("protocol", "")),
                "topology_hub": bool(sw_data.get("topology_hub")) or sw.get("protocol") == "proxmox",
                "device_id": str(sw.get("id") or ip),
                "child_devices": sw_data.get("child_devices", []),
                "cluster_name": sw_data.get("cluster_name", ""),
                "cluster_nodes": sw_data.get("cluster_nodes", []),
            }
            if configured_protocol == "proxmox":
                sw_entry["device_type"] = NodeRole.VIRTUALISATION_HOST
            switches_by_ip[ip] = sw_entry

            if sw_mac and role != NodeRole.VIRTUALISATION_HOST:
                mac_to_switch_ip[sw_mac] = ip

        # Identify the root router/gateway
        for sw in switch_configs:
            if not sw.get("enabled", True):
                continue
            ip = sw["ip"]
            sw_entry = switches_by_ip.get(ip)
            if not sw_entry:
                continue
            role = sw_entry.get("role")
            model_lower = str(sw_entry.get("model", "")).lower()
            name_lower = str(sw_entry.get("name", "")).lower()
            if (role == NodeRole.ROUTER or "router" in str(role).lower() or model_lower == "fritzbox" or "router" in name_lower or "gateway" in name_lower):
                root_ip = ip
                if sw_entry.get("mac"):
                    router_mac = sw_entry["mac"]
                break

        if not root_ip and switches_by_ip:
            root_ip = next((
                ip for ip, sw in switches_by_ip.items()
                if sw.get("role") != NodeRole.VIRTUALISATION_HOST
            ), None)

        all_switch_ips = {sw["ip"] for sw in switch_configs if sw.get("ip")}

        if root_ip and not router_mac and root_ip in switches_by_ip:
            router_mac = switches_by_ip[root_ip].get("mac", "")
            if router_mac:
                mac_to_switch_ip[router_mac] = root_ip

        # -------------------------------------------------------------
        # 2. Ingest Infrastructure & Access Point Devices
        # -------------------------------------------------------------
        infra_by_mac: Dict[str, Dict[str, Any]] = {}
        access_points: Dict[str, Dict[str, Any]] = {}

        # Configured access points
        for ap in access_points_config:
            ap_mac = normalize_mac(ap.get("mac", ""))
            if not ap_mac:
                continue
            ap_id = f"ap_{ap_mac}"
            access_points[ap_mac] = {
                "id": ap_id,
                "mac": ap_mac,
                "ip": ap.get("ip", ""),
                "name": ap.get("name", f"AP {ap_mac[-6:]}"),
                "type": NodeRole.ACCESS_POINT,
                "model": ap.get("model", "Access Point"),
                "vendor": lookup_vendor(ap_mac, vendors, ieee_vendors),
                "parent_ip": ap.get("parent_ip", ""),
                "parent_port": normalize_port(ap.get("parent_port", "")),
                "status": "online",
            }

        # Infrastructure devices
        for dev in infra_devices:
            norm_mac = normalize_mac(dev.get("mac", ""))
            if not norm_mac or norm_mac in mac_to_switch_ip:
                continue
            dev_type = dev.get("type", "other")
            if dev_type in ("access_point", "ap", "repeater") and norm_mac not in access_points:
                access_points[norm_mac] = {
                    "id": f"ap_{norm_mac}",
                    "mac": norm_mac,
                    "ip": dev.get("ip", ""),
                    "name": dev["name"],
                    "type": NodeRole.ACCESS_POINT,
                    "model": dev.get("model", "Access Point"),
                    "vendor": lookup_vendor(norm_mac, vendors, ieee_vendors),
                    "parent_ip": dev.get("parent_ip", ""),
                    "parent_port": normalize_port(dev.get("parent_port", "")),
                    "status": "online",
                }
            else:
                infra_by_mac[norm_mac] = {
                    "id": norm_mac,
                    "mac": norm_mac,
                    "name": dev["name"],
                    "type": dev_type,
                    "vendor": lookup_vendor(norm_mac, vendors, ieee_vendors),
                    "status": "online",
                }
                if dev_type == "router" and not router_mac:
                    router_mac = norm_mac

        all_infra_ips = all_switch_ips | {
            ap.get("ip") for ap in access_points.values() if ap.get("ip")
        } | {
            dev.get("ip") for dev in infra_devices if dev.get("ip")
        }

        # Unmanaged switches (indexed by canonical parent_ip and normalized parent_port)
        unmanaged_by_port = {}
        for us in unmanaged_switches:
            if us.get("device_type") in ["phone", "ont"] or us.get("role") in ["phone", "ont"]:
                continue
            p_ip = us.get("parent_ip", "")
            p_port = normalize_port(us.get("parent_port", ""))
            if p_ip and p_port:
                unmanaged_by_port[(p_ip, p_port)] = {
                    "id": f"unmanaged_{p_ip}_{p_port}",
                    "name": us.get("name", "Unmanaged Switch"),
                    "parent_ip": p_ip,
                    "parent_port": p_port,
                    "status": switches_by_ip.get(p_ip, {}).get("status", "online"),
                }

        # Phone devices (Desk Phones with passthrough Ethernet mini switch)
        phones: Dict[str, Dict[str, Any]] = {}
        phone_by_ip: Dict[str, str] = {}
        phone_by_mac: Dict[str, str] = {}
        phone_by_id: Dict[str, str] = {}
        phone_by_port: Dict[Tuple[str, str], str] = {}

        def register_phone(dev_id: str, name: str, ip: str, mac: str, parent_ip: str, parent_port: str, is_mini_switch: bool = True, passthrough_port: str = "PC", status: str = "online"):
            norm_mac = normalize_mac(mac) if mac else ""
            clean_mac = norm_mac.replace(":", "").upper() if norm_mac else ""
            fmt_mac = ":".join(clean_mac[i:i + 2] for i in range(0, len(clean_mac), 2)) if clean_mac else ""
            clean_phone_ip = ip if ip and ip not in all_infra_ips else ""
            pid = str(dev_id or (f"phone_{clean_mac}" if clean_mac else (f"phone_{clean_phone_ip}" if clean_phone_ip else f"phone_{len(phones)}")))
            norm_pport = normalize_port(parent_port) if parent_port else ""
            phones[pid] = {
                "id": pid,
                "name": name or (f"Phone ({clean_phone_ip})" if clean_phone_ip else "Desk Phone"),
                "type": NodeRole.PHONE,
                "role": NodeRole.PHONE,
                "device_type": "phone",
                "ip": clean_phone_ip,
                "mac": fmt_mac,
                "parent_ip": parent_ip or "",
                "parent_port": norm_pport,
                "is_mini_switch": is_mini_switch,
                "passthrough": is_mini_switch,
                "passthrough_port": passthrough_port or "PC",
                "status": status,
            }
            phone_by_id[pid] = pid
            if clean_phone_ip:
                phone_by_ip[clean_phone_ip] = pid
            if norm_mac:
                phone_by_mac[norm_mac] = pid
            if clean_mac:
                phone_by_mac[clean_mac] = pid
            if fmt_mac:
                phone_by_mac[fmt_mac] = pid
            if parent_ip and norm_pport and is_mini_switch:
                phone_by_port[(parent_ip, norm_pport)] = pid

        # 1. Configured unified devices with device_type == "phone"
        unified_devices = config.get("devices", [])
        for dev in unified_devices:
            if dev.get("device_type") == "phone" or dev.get("role") == "phone":
                register_phone(
                    dev_id=dev.get("id", ""),
                    name=dev.get("name", "Desk Phone"),
                    ip=dev.get("ip", ""),
                    mac=dev.get("mac", ""),
                    parent_ip=dev.get("parent_ip", ""),
                    parent_port=dev.get("parent_port", ""),
                    is_mini_switch=dev.get("is_mini_switch", True),
                    passthrough_port=dev.get("passthrough_port", "PC"),
                    status="online",
                )

        # 2. Configured unmanaged_switches with device_type == "phone"
        for us in unmanaged_switches:
            if us.get("device_type") == "phone" or us.get("role") == "phone":
                register_phone(
                    dev_id=us.get("id", ""),
                    name=us.get("name", "Desk Phone"),
                    ip=us.get("ip", ""),
                    mac=us.get("mac", ""),
                    parent_ip=us.get("parent_ip", ""),
                    parent_port=us.get("parent_port", ""),
                    is_mini_switch=us.get("is_mini_switch", True),
                    passthrough_port=us.get("passthrough_port", "PC"),
                    status="online",
                )

        # 3. Configured infrastructure_devices with type == "phone"
        for dev in infra_devices:
            if dev.get("type") == "phone" or dev.get("device_type") == "phone":
                register_phone(
                    dev_id=dev.get("id", ""),
                    name=dev.get("name", "Desk Phone"),
                    ip=dev.get("ip", ""),
                    mac=dev.get("mac", ""),
                    parent_ip=dev.get("parent_ip", ""),
                    parent_port=dev.get("parent_port", ""),
                    is_mini_switch=dev.get("is_mini_switch", True),
                    passthrough_port=dev.get("passthrough_port", "PC"),
                    status="online",
                )

        # 4. Discovered or saved clients marked as phone mini switch or targeted as parent_ip by a switch
        all_parent_targets = {
            sw.get("parent_ip", "") for sw in switch_configs if sw.get("parent_ip") and sw.get("parent_ip") not in all_infra_ips
        } | {
            us.get("parent_ip", "") for us in unmanaged_switches if us.get("parent_ip") and us.get("parent_ip") not in all_infra_ips
        }
        all_candidate_clients: Dict[str, Dict[str, Any]] = {}
        for cmac, cinfo in discovered_db.items():
            clean_m = str(cmac).replace(":", "").replace("-", "").upper()
            all_candidate_clients[clean_m] = dict(cinfo)
        for cmac, cinfo in db_clients.items():
            clean_m = str(cmac).replace(":", "").replace("-", "").upper()
            if clean_m in all_candidate_clients:
                all_candidate_clients[clean_m].update(cinfo)
            else:
                all_candidate_clients[clean_m] = dict(cinfo)

        for clean_m, cinfo in all_candidate_clients.items():
            norm_m = normalize_mac(cinfo.get("mac", clean_m))
            cip = cinfo.get("ip", "")
            if cip in all_infra_ips:
                cip = ""
            dev_type = cinfo.get("device_type", "")
            is_mini = bool(cinfo.get("is_mini_switch") or cinfo.get("passthrough"))
            is_targeted = bool(all_parent_targets and (norm_m in all_parent_targets or clean_m in all_parent_targets or (cip and cip in all_parent_targets) or f"phone_{clean_m}" in all_parent_targets))
            if (dev_type == "phone" and (is_mini or is_targeted)) or (is_mini and dev_type in (None, "", "phone", "client")):
                if norm_m not in phone_by_mac and clean_m not in phone_by_mac and (not cip or cip not in phone_by_ip):
                    p_ip = cinfo.get("switch_ip") or cinfo.get("parent_ip") or ""
                    if not p_ip and cinfo.get("ip") in all_infra_ips:
                        p_ip = cinfo.get("ip")
                    p_port = cinfo.get("port") or cinfo.get("parent_port") or ""
                    register_phone(
                        dev_id=f"phone_{clean_m}",
                        name=cinfo.get("hostname") or cinfo.get("host") or cinfo.get("vendor") or f"Phone ({cip or norm_m[-8:]})",
                        ip=cip,
                        mac=norm_m,
                        parent_ip=p_ip,
                        parent_port=p_port,
                        is_mini_switch=True,
                        passthrough_port=cinfo.get("passthrough_port", "PC"),
                        status=cinfo.get("status", "online"),
                    )

        def resolve_phone_id(target_key: str) -> Optional[str]:
            if not target_key:
                return None
            if target_key in switches_by_ip or target_key in all_infra_ips or target_key in access_points:
                return None
            if target_key in phone_by_id:
                return phone_by_id[target_key]
            if target_key in phone_by_ip:
                return phone_by_ip[target_key]
            norm_m = normalize_mac(target_key)
            if norm_m in phone_by_mac:
                return phone_by_mac[norm_m]
            clean_m = target_key.replace(":", "").replace("-", "").upper()
            if clean_m in phone_by_mac:
                return phone_by_mac[clean_m]
            return None

        def is_switch_mac(m: str) -> bool:
            return m in mac_to_switch_ip

        def is_ap_mac(m: str) -> bool:
            return m in access_points

        def is_infra_mac(m: str) -> bool:
            return m in infra_by_mac

        def is_phone_mac(m: str) -> bool:
            return m in phone_by_mac

        physical_hosts: Dict[str, Dict[str, Any]] = {}
        physical_host_ids: Dict[Tuple[str, str], str] = {}
        cluster_default_host_ids: Dict[str, str] = {}
        for cluster_ip, sw in switches_by_ip.items():
            if sw.get("role") != NodeRole.VIRTUALISATION_HOST:
                continue
            cluster_nodes = [
                node for node in sw.get("cluster_nodes", [])
                if isinstance(node, dict) and node.get("name")
            ]
            if not cluster_nodes:
                continue
            local_node = next((node for node in cluster_nodes if node.get("local")), None)
            if not local_node:
                local_node = next((
                    node for node in cluster_nodes
                    if cluster_ip in {
                        str(node.get("ip") or "").split("/", 1)[0],
                        *(str(ip).split("/", 1)[0] for ip in node.get("ips", [])),
                    }
                ), None)
            if not local_node:
                local_node = cluster_nodes[0]

            for cluster_node in cluster_nodes:
                node_name = str(cluster_node["name"])
                node_id = str(
                    cluster_node.get("id")
                    or f"proxmox:{sw.get('device_id', cluster_ip)}:node:{node_name}"
                )
                canonical_name = _canonical_node_name(node_name)
                if canonical_name:
                    physical_host_ids[(cluster_ip, canonical_name)] = node_id
                node_name_lower = node_name.lower()
                if node_name_lower:
                    physical_host_ids[(cluster_ip, node_name_lower)] = node_id
                if cluster_node is local_node:
                    cluster_default_host_ids[cluster_ip] = node_id
                elif cluster_ip not in cluster_default_host_ids:
                    cluster_default_host_ids[cluster_ip] = node_id
                host_ips = []
                for value in [cluster_node.get("ip"), *cluster_node.get("ips", [])]:
                    ip_value = str(value or "").split("/", 1)[0]
                    if ip_value and ip_value not in host_ips:
                        host_ips.append(ip_value)
                host_macs = []
                for value in [cluster_node.get("mac"), *cluster_node.get("macs", [])]:
                    mac = normalize_mac(value)
                    if mac and mac not in host_macs:
                        host_macs.append(mac)
                if cluster_node is local_node and sw.get("mac") and sw["mac"] not in host_macs:
                    host_macs.append(sw["mac"])

                # Scanner inventory and ARP-capable protocol tables fill the gap when
                # Proxmox does not expose an interface hardware address.
                combined_clients = {**discovered_db, **db_clients}
                for client_mac, client in combined_clients.items():
                    client_ip = str(
                        client.get("ip")
                        or client.get("scanner_ip")
                        or client.get("last_seen_ip")
                        or ""
                    ).split("/", 1)[0]
                    mac = normalize_mac(client.get("mac") or client_mac)
                    if client_ip in host_ips and mac and mac not in host_macs:
                        host_macs.append(mac)
                for candidate_sw in switches_by_ip.values():
                    for entry in candidate_sw.get("mac_table", []):
                        entry_ip = str(entry.get("ip") or "").split("/", 1)[0]
                        mac = normalize_mac(entry.get("mac", ""))
                        if entry_ip in host_ips and mac and mac not in host_macs:
                            host_macs.append(mac)

                physical_hosts[node_id] = {
                    **cluster_node,
                    "id": node_id,
                    "name": node_name,
                    "ip": host_ips[0] if host_ips else (cluster_ip if cluster_node is local_node else ""),
                    "ips": host_ips,
                    "mac": host_macs[0] if host_macs else "",
                    "macs": host_macs,
                    "cluster_ip": cluster_ip,
                    "topology_hub_id": sw.get("device_id", cluster_ip),
                    "cluster_name": sw.get("cluster_name", ""),
                    "cluster_nodes": cluster_nodes,
                }

        physical_host_macs = {
            mac
            for host in physical_hosts.values()
            for mac in host.get("macs", [])
        }

        child_macs = {
            normalize_mac(mac)
            for sw in switches_by_ip.values()
            for child in sw.get("child_devices", [])
            if isinstance(child, dict)
            for mac in child.get("macs", [])
            if normalize_mac(mac)
        }
        child_macs.update(physical_host_macs)
        child_names = {
            str(child.get("name") or "").strip().lower()
            for sw in switches_by_ip.values()
            for child in sw.get("child_devices", [])
            if isinstance(child, dict) and str(child.get("name") or "").strip()
        }
        child_ips = {
            str(ip).split("/", 1)[0]
            for sw in switches_by_ip.values()
            for child in sw.get("child_devices", [])
            if isinstance(child, dict)
            for ip in child.get("ips", [])
            if str(ip).split("/", 1)[0]
        }

        # -------------------------------------------------------------
        # 3. Build Normalized Port MAC Tables (sw_port_macs[ip][norm_port] = [macs])
        # -------------------------------------------------------------
        sw_port_macs: Dict[str, Dict[str, List[str]]] = {}
        for ip, sw in switches_by_ip.items():
            sw_port_macs[ip] = {}
            for entry in sw.get("mac_table", []):
                port = normalize_port(entry.get("port", ""))
                mac = normalize_mac(entry.get("mac", ""))
                if port and mac:
                    if is_ignored_mac(mac, config):
                        continue
                    if port not in sw_port_macs[ip]:
                        sw_port_macs[ip][port] = []
                    if mac not in sw_port_macs[ip][port]:
                        sw_port_macs[ip][port].append(mac)

        # -------------------------------------------------------------
        # 4. Multi-Hop Switch Chain & Transit Elimination Algorithm
        # -------------------------------------------------------------
        # Helper to check if s_ip sees t_ip directly or via downstream devices
        def switch_sees_switch(s_ip: str, t_ip: str) -> Tuple[bool, Optional[str]]:
            s_ports = sw_port_macs.get(s_ip, {})
            t_mac = switches_by_ip.get(t_ip, {}).get("mac", "")
            if t_mac:
                for p, macs in s_ports.items():
                    if t_mac in macs:
                        return True, p
            # Check overlap on non-uplink / leaf ports of t_ip
            if t_ip in sw_port_macs:
                for t_p, t_macs in sw_port_macs[t_ip].items():
                    for s_p, s_macs in s_ports.items():
                        overlap = set(s_macs).intersection(t_macs)
                        if router_mac:
                            overlap.discard(router_mac)
                        s_mac = switches_by_ip.get(s_ip, {}).get("mac")
                        if s_mac:
                            overlap.discard(s_mac)
                        if overlap:
                            return True, s_p
            return False, None

        # Direct links: list of (src_ip, normalized_port, dst_ip)
        direct_switch_links: List[Tuple[str, str, str]] = []
        resolved_link_pairs: Set[Tuple[str, str]] = set()

        # Priority 4A: LLDP / CDP Neighbor Discovery Peering
        for ip, sw in switches_by_ip.items():
            neighbors = sw.get("neighbors", [])
            for n in neighbors:
                local_port = normalize_port(n.get("local_port", ""))
                remote_chassis = normalize_mac(n.get("remote_chassis_id", ""))
                if remote_chassis in mac_to_switch_ip:
                    remote_ip = mac_to_switch_ip[remote_chassis]
                    if remote_ip != ip:
                        pair_key = tuple(sorted([ip, remote_ip]))
                        if pair_key not in resolved_link_pairs:
                            direct_switch_links.append((ip, local_port, remote_ip))
                            resolved_link_pairs.add(pair_key)

        # Priority 4B: Forwarding Database (FDB) Transit-Hop Elimination
        port_candidates: Dict[Tuple[str, str], List[str]] = {}
        for src_ip, ports in sw_port_macs.items():
            # Skip routers: a router's ARP table is an L3 cache, not an L2 physical switch forwarding database
            if switches_by_ip.get(src_ip, {}).get("role") in {
                NodeRole.ROUTER,
                NodeRole.VIRTUALISATION_HOST,
            } or src_ip == root_ip:
                continue
            src_mac = switches_by_ip[src_ip]["mac"]

            for port, macs in ports.items():
                switches_on_port = set()
                for m in macs:
                    if is_switch_mac(m) and mac_to_switch_ip[m] != src_ip:
                        switches_on_port.add(mac_to_switch_ip[m])

                # Also check downstream MACs: if this port sees downstream devices of another switch
                for other_ip, other_sw in switches_by_ip.items():
                    if other_ip == src_ip or other_ip in switches_on_port:
                        continue
                    if other_ip == root_ip or other_sw.get("role") == NodeRole.ROUTER:
                        continue
                    sees_it, found_port = switch_sees_switch(src_ip, other_ip)
                    if sees_it and found_port == port:
                        switches_on_port.add(other_ip)

                if not switches_on_port:
                    continue

                direct_neighbors = []
                for t_ip in switches_on_port:
                    t_role = switches_by_ip.get(t_ip, {}).get("role")
                    is_t_router = (t_role == NodeRole.ROUTER or t_ip == root_ip)

                    # Mutual visibility check: If t_ip is NOT the router, t_ip must also see src_ip
                    if not is_t_router:
                        sees_src, _ = switch_sees_switch(t_ip, src_ip)
                        if not sees_src:
                            continue

                    is_behind_any = False
                    for u_ip in switches_on_port:
                        if u_ip == t_ip:
                            continue
                        u_role = switches_by_ip.get(u_ip, {}).get("role")
                        is_u_router = (u_role == NodeRole.ROUTER or u_ip == root_ip)

                        # Router transit elimination: If T is root router and U is another switch on the same port:
                        if is_t_router and not is_u_router:
                            u_sees_router, u_r_port = switch_sees_switch(u_ip, root_ip)
                            u_sees_src, u_s_port = switch_sees_switch(u_ip, src_ip)
                            if u_sees_router and u_sees_src and u_r_port != u_s_port:
                                is_behind_any = True
                                break
                            elif u_sees_router and not u_sees_src:
                                is_behind_any = True
                                break

                        # General transit elimination: check if T is behind U relative to src_ip
                        u_sees_t, port_u_t = switch_sees_switch(u_ip, t_ip)
                        u_sees_s, port_u_s = switch_sees_switch(u_ip, src_ip)
                        if port_u_t and port_u_s and port_u_t != port_u_s:
                            is_behind_any = True
                            break

                    if not is_behind_any:
                        direct_neighbors.append(t_ip)

                if direct_neighbors:
                    port_candidates[(src_ip, port)] = direct_neighbors

        # Confirm direct links (mutual confirmation for switches, direct link for router)
        for (src_ip, port), candidates in port_candidates.items():
            for dst_ip in candidates:
                pair_key = tuple(sorted([src_ip, dst_ip]))
                if pair_key in resolved_link_pairs:
                    continue

                dst_role = switches_by_ip.get(dst_ip, {}).get("role")
                is_router = (dst_role == NodeRole.ROUTER or dst_ip == root_ip)

                if is_router:
                    direct_switch_links.append((src_ip, port, dst_ip))
                    resolved_link_pairs.add(pair_key)
                else:
                    # Mutual selection: Verify dst_ip also chose src_ip on some port
                    dst_chose_src = any(
                        src_ip in c_list
                        for (d_ip, _), c_list in port_candidates.items()
                        if d_ip == dst_ip
                    )
                    if dst_chose_src:
                        direct_switch_links.append((src_ip, port, dst_ip))
                        resolved_link_pairs.add(pair_key)

        # Static uplinks from config (ports normalized)
        static_uplinks = {}
        for sw in switch_configs:
            if not sw.get("enabled", True):
                continue
            ip = sw["ip"]
            if switches_by_ip.get(ip, {}).get("topology_hub"):
                continue
            p_ip = sw.get("parent_ip", "")
            p_port = normalize_port(sw.get("parent_port", ""))
            up_port = normalize_port(sw.get("uplink_port", ""))
            if p_ip:
                static_uplinks[ip] = {
                    "parent_ip": p_ip,
                    "parent_port": p_port,
                    "uplink_port": up_port,
                }

        # Build bidirectional switch links with telemetry
        processed_links = set()
        links: List[Dict[str, Any]] = []

        # 1. Process static uplinks
        for child_ip, info in static_uplinks.items():
            p_ip = info["parent_ip"]
            p_port = info["parent_port"]
            up_port = info["uplink_port"]

            if child_ip not in switches_by_ip:
                continue

            phone_parent_id = resolve_phone_id(p_ip)
            if phone_parent_id:
                phone_node = phones[phone_parent_id]
                source_port_label = format_port_display(p_port) if (p_port and p_port != "unknown") else phone_node.get("passthrough_port", "PC")
                target_port_label = format_port_display(up_port) if up_port else "Uplink"
                speed = "1G"
                tx_bps = 0
                rx_bps = 0
                if up_port and child_ip in switches_by_ip:
                    for p_info in switches_by_ip[child_ip]["ports"]:
                        if ports_equal(p_info.get("port"), up_port):
                            speed = p_info.get("speed", "1G")
                            tx_bps = p_info.get("speed_tx_bps", 0)
                            rx_bps = p_info.get("speed_rx_bps", 0)
                            break
                links.append({
                    "id": f"link_{phone_parent_id}_{child_ip}",
                    "source": phone_parent_id,
                    "target": child_ip,
                    "source_port": source_port_label,
                    "target_port": target_port_label,
                    "speed": speed,
                    "tx_bps": tx_bps,
                    "rx_bps": rx_bps,
                    "capacity_bps": parse_speed_bps(speed),
                    "utilization_pct": 0.0,
                    "type": "uplink",
                    "status": "online",
                })
                processed_links.add(tuple(sorted([phone_parent_id, child_ip])))
                continue

            # Deduce ports if not explicitly specified
            if not p_port:
                _, p_port = switch_sees_switch(p_ip, child_ip)
                if not p_port:
                    p_port = "LAN" if switches_by_ip.get(p_ip, {}).get("role") == NodeRole.ROUTER else "unknown"
            if not up_port:
                _, up_port = switch_sees_switch(child_ip, p_ip)
                if not up_port:
                    up_port = "uplink"

            speed = "1G"
            tx_bps = 0
            rx_bps = 0

            if p_ip in switches_by_ip:
                for p_info in switches_by_ip[p_ip]["ports"]:
                    if ports_equal(p_info.get("port"), p_port):
                        speed = p_info.get("speed", "1G")
                        tx_bps = p_info.get("speed_tx_bps", 0)
                        rx_bps = p_info.get("speed_rx_bps", 0)
                        break
            elif child_ip in switches_by_ip and up_port:
                for p_info in switches_by_ip[child_ip]["ports"]:
                    if ports_equal(p_info.get("port"), up_port):
                        speed = p_info.get("speed", "1G")
                        tx_bps = p_info.get("speed_tx_bps", 0)
                        rx_bps = p_info.get("speed_rx_bps", 0)
                        break

            source_port_label = format_port_display(p_port)
            target_port_label = format_port_display(up_port) if up_port else "unknown"

            capacity_bps = parse_speed_bps(speed)
            utilization_pct = round((max(tx_bps, rx_bps) / capacity_bps) * 100, 2) if capacity_bps > 0 else 0.0

            links.append({
                "id": f"link_{p_ip}_{child_ip}",
                "source": p_ip,
                "target": child_ip,
                "source_port": source_port_label,
                "target_port": target_port_label,
                "speed": speed,
                "tx_bps": tx_bps,
                "rx_bps": rx_bps,
                "capacity_bps": capacity_bps,
                "utilization_pct": min(utilization_pct, 100.0),
                "type": "uplink",
                "status": "online",
            })
            processed_links.add(tuple(sorted([p_ip, child_ip])))

        # 2. Process discovered direct switch links
        switch_link_ports: Dict[Tuple[str, str], str] = {}
        for src_ip, port, dst_ip in direct_switch_links:
            switch_link_ports[(src_ip, dst_ip)] = normalize_port(port)

        for src_ip, port, dst_ip in direct_switch_links:
            link_key = tuple(sorted([src_ip, dst_ip]))
            if link_key in processed_links:
                continue

            # If either switch has a static parent configured that contradicts this link, skip it
            if src_ip in static_uplinks and static_uplinks[src_ip]["parent_ip"] != dst_ip:
                continue
            if dst_ip in static_uplinks and static_uplinks[dst_ip]["parent_ip"] != src_ip:
                continue

            processed_links.add(link_key)
            dst_port = switch_link_ports.get((dst_ip, src_ip), "")

            # If reverse port is unknown, deduce it from dst_ip's MAC table
            if not dst_port:
                dst_ports = sw_port_macs.get(dst_ip, {})
                src_mac = switches_by_ip[src_ip]["mac"]
                for dp, dp_macs in dst_ports.items():
                    if src_mac in dp_macs:
                        dst_port = dp
                        break
                if not dst_port:
                    _, dst_port = switch_sees_switch(dst_ip, src_ip)

            speed = "1G"
            tx_bps = 0
            rx_bps = 0
            for p_info in switches_by_ip[src_ip]["ports"]:
                if ports_equal(p_info.get("port"), port):
                    speed = p_info.get("speed", "1G")
                    tx_bps = p_info.get("speed_tx_bps", 0)
                    rx_bps = p_info.get("speed_rx_bps", 0)
                    break

            if tx_bps == 0 and rx_bps == 0 and dst_ip in switches_by_ip and dst_port:
                for p_info in switches_by_ip[dst_ip].get("ports", []):
                    if ports_equal(p_info.get("port"), dst_port):
                        if speed == "1G" and p_info.get("speed"):
                            speed = p_info.get("speed")
                        tx_bps = p_info.get("speed_rx_bps", 0)
                        rx_bps = p_info.get("speed_tx_bps", 0)
                        break

            capacity_bps = parse_speed_bps(speed)
            utilization_pct = round((max(tx_bps, rx_bps) / capacity_bps) * 100, 2) if capacity_bps > 0 else 0.0

            if switches_by_ip.get(dst_ip, {}).get("role") == NodeRole.ROUTER:
                if not dst_port or str(dst_port).lower().startswith(("vlan", "lo", "enc", "wg", "tun", "tap", "pppoe")):
                    target_port_label = format_port_display(dst_port) if dst_port else "LAN"
                else:
                    target_port_label = format_port_display(dst_port)
            else:
                target_port_label = format_port_display(dst_port) if dst_port else "unknown"

            links.append({
                "id": f"link_{src_ip}_{dst_ip}",
                "source": src_ip,
                "target": dst_ip,
                "source_port": format_port_display(port),
                "target_port": target_port_label,
                "speed": speed,
                "tx_bps": tx_bps,
                "rx_bps": rx_bps,
                "capacity_bps": capacity_bps,
                "utilization_pct": min(utilization_pct, 100.0),
                "type": "uplink",
                "status": "online",
            })

        # -------------------------------------------------------------
        # 5. Physical Virtualisation Host Attachment
        # -------------------------------------------------------------
        occupied_switch_ports: Set[Tuple[str, str]] = set()
        for link in links:
            if link.get("type") not in {"uplink"}:
                continue
            src = str(link.get("source") or "")
            dst = str(link.get("target") or "")
            src_port = normalize_port(link.get("source_port", ""))
            dst_port = normalize_port(link.get("target_port", ""))
            if src in switches_by_ip and src_port and src_port not in {"unknown", "uplink", "lan"}:
                occupied_switch_ports.add((src, src_port))
            if dst in switches_by_ip and dst_port and dst_port not in {"unknown", "uplink", "lan"}:
                occupied_switch_ports.add((dst, dst_port))

        for host_id, host in physical_hosts.items():
            interface_by_mac = {
                normalize_mac(interface.get("mac", "")): interface.get("name", "")
                for interface in host.get("interfaces", [])
                if normalize_mac(interface.get("mac", ""))
            }
            candidates = []
            for switch_ip, ports in sw_port_macs.items():
                if switches_by_ip.get(switch_ip, {}).get("role") == NodeRole.VIRTUALISATION_HOST:
                    continue
                for port, port_macs in ports.items():
                    if (switch_ip, port) in occupied_switch_ports:
                        continue
                    switch_role = switches_by_ip.get(switch_ip, {}).get("role")
                    if switch_role == NodeRole.ROUTER and str(port).lower().startswith(
                        ("vlan", "lo", "enc", "wg", "tun", "tap", "pppoe")
                    ):
                        continue
                    matched_macs = [mac for mac in host.get("macs", []) if mac in port_macs]
                    if not matched_macs:
                        continue
                    other_switch_count = sum(
                        1
                        for mac in port_macs
                        if is_switch_mac(mac) and mac_to_switch_ip.get(mac) != switch_ip
                    )
                    candidates.append((switch_ip, port, matched_macs[0], other_switch_count))

            if not candidates:
                cached = self._physical_host_link_cache.get(host_id)
                if cached:
                    cached_switch_ip, cached_port = cached
                    cached_port_macs = sw_port_macs.get(cached_switch_ip, {}).get(cached_port, [])
                    other_switches_on_cached = any(
                        is_switch_mac(mac) and mac_to_switch_ip.get(mac) != cached_switch_ip
                        for mac in cached_port_macs
                    )
                    if (
                        cached_switch_ip in switches_by_ip
                        and switches_by_ip.get(cached_switch_ip, {}).get("role") != NodeRole.ROUTER
                        and (cached_switch_ip, cached_port) not in occupied_switch_ports
                        and not other_switches_on_cached
                    ):
                        candidates = [(cached_switch_ip, cached_port, "", 0)]
                    else:
                        self._physical_host_link_cache.pop(host_id, None)
                if not candidates:
                    continue

            non_router_candidates = [
                candidate
                for candidate in candidates
                if switches_by_ip.get(candidate[0], {}).get("role") != NodeRole.ROUTER
            ]
            # Do not auto-attach physical hosts to router candidates. Router ARP/VLAN
            # visibility is not a reliable physical-port signal for host placement.
            # If a host is directly connected to a router, use explicit parent config.
            pool = non_router_candidates
            if not pool:
                continue

            # Prioritize direct edge switch ports (no downstream transit switches learned on port)
            direct_candidates = [c for c in pool if c[3] == 0]
            if direct_candidates:
                pool = direct_candidates

            def host_link_rank(candidate: Tuple[str, str, str, int]):
                switch_ip, port, _, other_switch_count = candidate
                role = switches_by_ip.get(switch_ip, {}).get("role")
                is_router = (role == NodeRole.ROUTER or switch_ip == root_ip)
                learned_count = len(sw_port_macs.get(switch_ip, {}).get(port, []))
                return (
                    1 if is_router else 0,
                    other_switch_count,
                    learned_count,
                    switch_ip,
                    port,
                )

            switch_ip, port, host_mac, other_switch_count = min(pool, key=host_link_rank)
            # Only cache true direct edge connections to avoid trunk port caching
            if other_switch_count == 0:
                self._physical_host_link_cache[host_id] = (switch_ip, port)
            elif host_id not in self._physical_host_link_cache and not other_switch_count:
                self._physical_host_link_cache[host_id] = (switch_ip, port)
            link_key = tuple(sorted([switch_ip, host_id]))
            if link_key in processed_links:
                continue
            processed_links.add(link_key)
            speed = "1G"
            tx_bps = 0
            rx_bps = 0
            for port_info in switches_by_ip[switch_ip].get("ports", []):
                if ports_equal(port_info.get("port"), port):
                    speed = port_info.get("speed", "1G")
                    tx_bps = port_info.get("speed_tx_bps", 0)
                    rx_bps = port_info.get("speed_rx_bps", 0)
                    break
            capacity_bps = parse_speed_bps(speed)
            links.append({
                "id": f"link_{switch_ip}_{host_id}_{port}",
                "source": switch_ip,
                "target": host_id,
                "source_port": format_port_display(port),
                "target_port": interface_by_mac.get(host_mac) or "Host",
                "speed": speed,
                "tx_bps": tx_bps,
                "rx_bps": rx_bps,
                "capacity_bps": capacity_bps,
                "utilization_pct": round((max(tx_bps, rx_bps) / capacity_bps) * 100, 2) if capacity_bps else 0.0,
                "type": "physical_host",
                "status": "online" if host.get("status") == "online" else host.get("status", "unknown"),
            })

        # -------------------------------------------------------------
        # 6. Access Points (APs) Attachment & Downstream Client Bridging
        # -------------------------------------------------------------
        ap_connections: Dict[str, Tuple[str, str]] = {} # ap_mac -> (switch_ip, normalized_port)
        clients_behind_ap: Dict[str, List[str]] = {}    # ap_mac -> [client_macs]

        for ap_mac, ap_info in access_points.items():
            clients_behind_ap[ap_mac] = []
            # Check static parent config
            p_ip = ap_info.get("parent_ip")
            p_port = normalize_port(ap_info.get("parent_port"))
            if p_ip and p_port and p_ip in switches_by_ip:
                ap_connections[ap_mac] = (p_ip, p_port)
                continue

            # Auto-locate AP on switch ports: Find deepest switch port with ap_mac
            candidate_ports = []
            for ip, ports in sw_port_macs.items():
                for port, macs in ports.items():
                    if ap_mac in macs:
                        has_other_switch = any(is_switch_mac(m) for m in macs)
                        candidate_ports.append((ip, port, has_other_switch))

            # Prioritize ports without other switches (edge ports)
            leaf_candidates = [c for c in candidate_ports if not c[2]]
            if leaf_candidates:
                ap_connections[ap_mac] = (leaf_candidates[0][0], leaf_candidates[0][1])
            elif candidate_ports:
                ap_connections[ap_mac] = (candidate_ports[0][0], candidate_ports[0][1])

        # Attribute downstream Wi-Fi clients to AP
        for ap_mac, conn in ap_connections.items():
            sw_ip, sw_port = conn
            port_macs = sw_port_macs.get(sw_ip, {}).get(sw_port, [])
            for m in port_macs:
                if m != ap_mac and not is_switch_mac(m) and not is_ap_mac(m) and not is_infra_mac(m) and m not in physical_host_macs:
                    clients_behind_ap[ap_mac].append(m)

        # -------------------------------------------------------------
        # 7. Infrastructure Device Connections
        # -------------------------------------------------------------
        infra_connections: Dict[str, Tuple[str, str]] = {}
        for mac, dev in infra_by_mac.items():
            candidates = []
            for ip, ports in sw_port_macs.items():
                for port, macs in ports.items():
                    if mac in macs:
                        has_switch = any(is_switch_mac(m) for m in macs)
                        if not has_switch:
                            candidates.append((ip, port))
            if candidates:
                infra_connections[mac] = candidates[0]

        # -------------------------------------------------------------
        # 8. Client Placement across Multi-Switch Chains
        # -------------------------------------------------------------
        clients: Dict[str, Dict[str, Any]] = {}
        client_links: List[Dict[str, Any]] = []
        all_known_client_macs = set(db_clients.keys()) | set(discovered_db.keys())
        active_macs_to_port: Dict[str, Tuple[str, str]] = {}

        # Track inter-switch trunk ports to prevent transit clients from being assigned to trunk ports
        inter_switch_ports: Set[Tuple[str, str]] = set()
        for l in links:
            s_node = l.get("source")
            t_node = l.get("target")
            s_port = normalize_port(l.get("source_port", ""))
            t_port = normalize_port(l.get("target_port", ""))
            if s_node in switches_by_ip and s_port:
                inter_switch_ports.add((s_node, s_port))
            if t_node in switches_by_ip and t_port:
                inter_switch_ports.add((t_node, t_port))

        # Resolve physical switch attachment port for all phones
        for pid, phone in phones.items():
            pmac = phone.get("mac")
            p_ip = phone.get("parent_ip")
            p_port = phone.get("parent_port")

            # If not statically attached to a switch, find its leaf switch port in sw_port_macs
            if (not p_ip or not p_port or p_ip not in switches_by_ip) and pmac:
                clean_pmac = normalize_mac(pmac)
                best_loc = None
                for sw_ip, ports in sw_port_macs.items():
                    for p_num, macs in ports.items():
                        if (sw_ip, p_num) in inter_switch_ports:
                            continue
                        if clean_pmac in macs or any(normalize_mac(m) == clean_pmac for m in macs):
                            has_other_switch = any(is_switch_mac(m) and mac_to_switch_ip[m] != sw_ip and mac_to_switch_ip[m] != root_ip for m in macs)
                            if not has_other_switch:
                                is_router = (
                                    switches_by_ip.get(sw_ip, {}).get("role") == NodeRole.ROUTER
                                    or "router" in str(switches_by_ip.get(sw_ip, {}).get("role", "")).lower()
                                    or str(switches_by_ip.get(sw_ip, {}).get("model", "")).lower() == "fritzbox"
                                )
                                if is_router and str(p_num).lower().startswith(("vlan", "wg", "tun", "tap", "enc", "lo", "wt")):
                                    continue
                                if not best_loc or (best_loc[2] and not is_router):
                                    best_loc = (sw_ip, normalize_port(p_num), is_router)
                if best_loc:
                    phone["parent_ip"] = best_loc[0]
                    phone["parent_port"] = best_loc[1]
                    p_ip = best_loc[0]
                    p_port = best_loc[1]

            if p_ip and p_port and phone.get("is_mini_switch"):
                phone_by_port[(p_ip, normalize_port(p_port))] = pid

        # Collect active clients from switch forwarding tables
        for ip, ports in sw_port_macs.items():
            for port, macs in ports.items():
                if (ip, port) in inter_switch_ports:
                    continue

                for mac in macs:
                    if is_switch_mac(mac) or is_ap_mac(mac) or is_infra_mac(mac) or is_phone_mac(mac) or mac in child_macs:
                        continue

                    claimed_by_ap = False
                    for ap_m, ap_clients in clients_behind_ap.items():
                        if mac in ap_clients:
                            claimed_by_ap = True
                            break
                    if claimed_by_ap:
                        all_known_client_macs.add(mac)
                        continue

                    # On access/leaf ports, ignore managed switches that aren't this switch
                    has_other_switch = any(is_switch_mac(m) and mac_to_switch_ip[m] != ip and mac_to_switch_ip[m] != root_ip for m in macs)
                    if not has_other_switch:
                        is_current_router = (
                            switches_by_ip.get(ip, {}).get("role") == NodeRole.ROUTER
                            or "router" in str(switches_by_ip.get(ip, {}).get("role", "")).lower()
                            or str(switches_by_ip.get(ip, {}).get("model", "")).lower() == "fritzbox"
                        )
                        # Do not attach client links directly to virtual/VLAN router sub-interfaces in topology
                        if is_current_router and str(port).lower().startswith(("vlan", "wg", "tun", "tap", "enc", "lo", "wt")):
                            continue
                        if mac in active_macs_to_port:
                            prev_ip, _ = active_macs_to_port[mac]
                            is_prev_router = (
                                switches_by_ip.get(prev_ip, {}).get("role") == NodeRole.ROUTER
                                or "router" in str(switches_by_ip.get(prev_ip, {}).get("role", "")).lower()
                                or str(switches_by_ip.get(prev_ip, {}).get("model", "")).lower() == "fritzbox"
                            )
                            if is_prev_router and not is_current_router:
                                active_macs_to_port[mac] = (ip, port)
                        else:
                            active_macs_to_port[mac] = (ip, port)
                            all_known_client_macs.add(mac)

        # Build client node records and links
        host_discovery = get_host_discovery_service()
        discovered_host_map = host_discovery.resolve_all_clients(switches_by_ip, infra_ips=all_infra_ips)

        for mac in all_known_client_macs:
            if is_ignored_mac(mac, config) or is_switch_mac(mac) or is_ap_mac(mac) or is_infra_mac(mac) or is_phone_mac(mac) or mac in child_macs:
                continue

            parent_ap_mac = None
            for ap_m, ap_clients in clients_behind_ap.items():
                if mac in ap_clients:
                    parent_ap_mac = ap_m
                    break

            clean_m = normalize_mac(mac)
            resolved_info = discovered_host_map.get(clean_m, {})

            client_entry = db_clients.get(mac, {})
            disc_entry = discovered_db.get(mac, {})

            # Distinguish user custom nickname from discovered network hostname
            custom_nickname = (client_entry.get("host") or "").strip()
            discovered_hostname = (resolved_info.get("hostname") or disc_entry.get("hostname") or "").strip()

            if custom_nickname and str(custom_nickname).strip().lower() in child_names:
                continue
            if discovered_hostname and str(discovered_hostname).strip().lower() in child_names:
                continue

            client_ip = str(
                resolved_info.get("ip")
                or client_entry.get("ip")
                or client_entry.get("scanner_ip")
                or disc_entry.get("ip")
                or ""
            ).split("/", 1)[0].strip()

            if client_ip in all_infra_ips:
                client_ip = ""
            if client_ip and client_ip in child_ips:
                continue

            vendor = disc_entry.get("vendor") or lookup_vendor(mac, vendors, ieee_vendors)
            formatted_mac = client_entry.get("mac") or disc_entry.get("mac") or ""
            if not formatted_mac:
                formatted_mac = ":".join(mac[i:i + 2] for i in range(0, len(mac), 2)).upper()

            # Display name hierarchy:
            # 1. User's custom nickname (if set)
            # 2. Discovered network hostname (DHCP / DNS / Hypervisor / UniFi)
            # 3. Hardware Vendor
            # 4. Fallback Client MAC
            if custom_nickname:
                display_name = custom_nickname
            elif discovered_hostname:
                display_name = discovered_hostname
            elif vendor:
                display_name = vendor
            else:
                display_name = f"Client {formatted_mac[-8:]}"

            dev_type = client_entry.get("device_type") or disc_entry.get("device_type") or ("smartphone" if "phone" in display_name.lower() else "laptop")
            last_seen_val = client_entry.get("last_seen") or disc_entry.get("last_seen") or 0

            if parent_ap_mac:
                ap_id = access_points[parent_ap_mac]["id"]
                clients[mac] = {
                    "id": mac,
                    "name": display_name,
                    "mac": formatted_mac,
                    "host": custom_nickname,
                    "hostname": discovered_hostname,
                    "type": NodeRole.CLIENT,
                    "device_type": dev_type,
                    "vendor": vendor,
                    "status": "online",
                    "parent_ap": parent_ap_mac,
                    "ip": client_ip,
                    "switch_ip": access_points[parent_ap_mac].get("ip", ""),
                    "port": "WLAN",
                    "last_seen_ip": access_points[parent_ap_mac].get("ip", ""),
                    "last_seen_port": "WLAN",
                    "last_seen_time": last_seen_val,
                }
                client_links.append({
                    "id": f"link_{ap_id}_{mac}",
                    "source": ap_id,
                    "target": mac,
                    "source_port": "WiFi",
                    "target_port": "",
                    "speed": "WiFi",
                    "tx_bps": 0,
                    "rx_bps": 0,
                    "capacity_bps": 1_000_000_000,
                    "utilization_pct": 0.0,
                    "type": "client",
                    "status": "online",
                })
                continue

            is_active = mac in active_macs_to_port
            if is_active:
                ip, port = active_macs_to_port[mac]
                status = "online"
            else:
                ip = client_entry.get("ip") or disc_entry.get("switch_ip") or ""
                port = normalize_port(client_entry.get("port") or disc_entry.get("port") or "")
                status = "offline"

            if not ip or not port:
                continue

            all_switch_ips = {sw["ip"] for sw in switch_configs}
            if ip in all_switch_ips and ip not in switches_by_ip:
                continue

            attached_infra = None
            for infra_mac, conn in infra_connections.items():
                if conn == (ip, port):
                    attached_infra = infra_mac
                    break

            clients[mac] = {
                "id": mac,
                "name": display_name,
                "mac": formatted_mac,
                "host": custom_nickname,
                "hostname": discovered_hostname,
                "type": NodeRole.CLIENT,
                "device_type": dev_type,
                "vendor": vendor,
                "status": status,
                "ip": client_ip,
                "switch_ip": ip,
                "port": port,
                "last_seen_ip": ip,
                "last_seen_port": port,
                "last_seen_time": last_seen_val,
            }

            if attached_infra:
                source_node = attached_infra
                source_port = ""
            elif (ip, port) in phone_by_port and mac not in phone_by_mac:
                phone_id = phone_by_port[(ip, port)]
                source_node = phone_id
                source_port = phones[phone_id].get("passthrough_port", "PC")
            elif (ip, port) in unmanaged_by_port:
                source_node = unmanaged_by_port[(ip, port)]["id"]
                source_port = ""
            else:
                source_node = ip
                source_port = format_port_display(port)

            speed = ""
            tx_bps = 0
            rx_bps = 0
            if not attached_infra and is_active and ip in switches_by_ip:
                for p_info in switches_by_ip[ip]["ports"]:
                    if ports_equal(p_info.get("port"), port):
                        speed = p_info.get("speed", "1G")
                        tx_bps = p_info.get("speed_tx_bps", 0)
                        rx_bps = p_info.get("speed_rx_bps", 0)
                        break
            elif not attached_infra and not is_active:
                speed = "offline"

            client_links.append({
                "id": f"link_{source_node}_{mac}",
                "source": source_node,
                "target": mac,
                "source_port": source_port,
                "target_port": "",
                "speed": speed or "1G",
                "tx_bps": tx_bps,
                "rx_bps": rx_bps,
                "capacity_bps": parse_speed_bps(speed),
                "utilization_pct": 0.0,
                "type": "client",
                "status": status,
            })

        # Persist discovered clients batch to SQLite
        try:
            self.device_repo.upsert_discovered_clients_batch(list(clients.values()))
        except Exception as e:
            logger.error(f"Error persisting discovered clients: {e}")

        # -------------------------------------------------------------
        # 9. Enrich Port Catalog with Roles & Connected Neighbors
        # -------------------------------------------------------------
        for ip, sw in switches_by_ip.items():
            enriched_ports: List[Dict[str, Any]] = []
            for p_info in sw.get("ports", []):
                p_raw = p_info.get("port", "")
                p_id = normalize_port(p_raw)
                p_speed = p_info.get("speed", "1G")
                p_link = p_info.get("link", "down")
                tx_bps = p_info.get("speed_tx_bps", 0)
                rx_bps = p_info.get("speed_rx_bps", 0)
                cap_bps = parse_speed_bps(p_speed)
                util_pct = round((max(tx_bps, rx_bps) / cap_bps) * 100, 2) if cap_bps > 0 else 0.0
                is_down = "down" in str(p_link).lower() or str(p_info.get("status", "")).lower() == "down"
                role = PortRole.UNUSED if is_down else PortRole.ACCESS
                connected_node_id = None
                connected_port_id = None

                # Check if this port is linked to another switch
                for l in links:
                    if l["source"] == ip and ports_equal(l["source_port"], p_id):
                        role = PortRole.DOWNLINK
                        connected_node_id = l["target"]
                        connected_port_id = l["target_port"]
                        break
                    elif l["target"] == ip and ports_equal(l["target_port"], p_id):
                        role = PortRole.UPLINK
                        connected_node_id = l["source"]
                        connected_port_id = l["source_port"]
                        break

                # Check if this port connects to an AP
                for ap_m, conn in ap_connections.items():
                    if conn[0] == ip and ports_equal(conn[1], p_id):
                        role = PortRole.AP_TRUNK
                        connected_node_id = access_points[ap_m]["id"]
                        connected_port_id = "Uplink"
                        break

                # Check if connected to unmanaged switch
                if (ip, p_id) in unmanaged_by_port:
                    role = PortRole.UNMANAGED_HUB
                    connected_node_id = unmanaged_by_port[(ip, p_id)]["id"]

                port_obj = EnhancedPortInfo(
                    port_id=p_id,
                    name=format_port_display(p_id),
                    link_status=p_link,
                    speed=p_speed,
                    duplex=p_info.get("duplex", "Full"),
                    role=role,
                    connected_node_id=connected_node_id,
                    connected_port_id=connected_port_id,
                    learned_macs=sw_port_macs.get(ip, {}).get(p_id, []),
                    telemetry=PortTelemetry(
                        tx_bps=tx_bps,
                        rx_bps=rx_bps,
                        speed_bps=cap_bps,
                        utilization_pct=min(util_pct, 100.0),
                        tx_packets=p_info.get("tx_packets", 0),
                        rx_packets=p_info.get("rx_packets", 0),
                    )
                )
                enriched_ports.append(port_obj.to_dict())
            sw["ports"] = enriched_ports

        # -------------------------------------------------------------
        # 10. Assemble Final Node Roster
        # -------------------------------------------------------------
        nodes: List[Dict[str, Any]] = []

        # Collect ONT unmanaged devices
        ont_devices: List[Dict[str, Any]] = []
        seen_ont_ids = set()
        for dev in unified_devices:
            if dev.get("device_type") == "ont" or dev.get("role") == "ont":
                oid = str(dev.get("id") or f"ont_{len(ont_devices)}")
                if oid not in seen_ont_ids:
                    seen_ont_ids.add(oid)
                    ont_devices.append(dict(dev))
        for dev in unmanaged_switches:
            if dev.get("device_type") == "ont" or dev.get("role") == "ont":
                oid = str(dev.get("id") or f"ont_{len(ont_devices)}")
                if oid not in seen_ont_ids:
                    seen_ont_ids.add(oid)
                    ont_devices.append(dict(dev))

        if ont_devices:
            nodes.append({
                "id": "internet",
                "name": "Internet",
                "type": NodeRole.INTERNET,
                "status": "online",
                "ont_count": len(ont_devices),
            })

            for idx, ont in enumerate(ont_devices):
                oid = str(ont.get("id") or f"ont_{idx}")
                name = ont.get("name") or f"ONT {idx + 1}"
                parent_ip = str(ont.get("parent_ip") or "").strip()
                parent_port = normalize_port(str(ont.get("parent_port") or ""))
                speed_down = str(ont.get("speed_down") or ont.get("speed") or "1G").strip()
                speed_up = str(ont.get("speed_up") or ont.get("speed") or "300M").strip()
                speed_display = f"{speed_down} / {speed_up}" if speed_down != speed_up else speed_down

                stats_device_ip = str(ont.get("stats_device_ip") or parent_ip).strip()
                stats_port = normalize_port(str(ont.get("stats_port") or "")) or parent_port

                # Query telemetry from stats_device_ip and stats_port
                tx_bps = 0
                rx_bps = 0
                tx_bytes = 0
                rx_bytes = 0
                if stats_device_ip in switches_by_ip and stats_port:
                    for p_info in switches_by_ip[stats_device_ip].get("ports", []):
                        if ports_equal(p_info.get("port"), stats_port) or ports_equal(p_info.get("port_id"), stats_port):
                            rx_bps = int(p_info.get("speed_rx_bps") or 0)
                            tx_bps = int(p_info.get("speed_tx_bps") or 0)
                            rx_bytes = int(p_info.get("bytes_recv") or 0)
                            tx_bytes = int(p_info.get("bytes_sent") or 0)
                            break

                cap_down_bps = parse_speed_bps(speed_down)
                cap_up_bps = parse_speed_bps(speed_up)
                util_down_pct = round((rx_bps / cap_down_bps) * 100, 2) if cap_down_bps > 0 else 0.0
                util_up_pct = round((tx_bps / cap_up_bps) * 100, 2) if cap_up_bps > 0 else 0.0
                overall_util_pct = max(util_down_pct, util_up_pct)

                ont_status = switches_by_ip.get(parent_ip, {}).get("status", "online") if parent_ip in switches_by_ip else "online"

                nodes.append({
                    "id": oid,
                    "name": name,
                    "type": NodeRole.ONT,
                    "role": NodeRole.ONT,
                    "device_type": "ont",
                    "status": ont_status,
                    "speed": speed_display,
                    "speed_down": speed_down,
                    "speed_up": speed_up,
                    "capacity_down_bps": cap_down_bps,
                    "capacity_up_bps": cap_up_bps,
                    "capacity_bps": max(cap_down_bps, cap_up_bps),
                    "parent_ip": parent_ip,
                    "parent_port": format_port_display(parent_port),
                    "stats_device_ip": stats_device_ip,
                    "stats_port": format_port_display(stats_port),
                    "rx_bps": rx_bps,
                    "tx_bps": tx_bps,
                    "rx_bytes": rx_bytes,
                    "tx_bytes": tx_bytes,
                    "utilization_down_pct": util_down_pct,
                    "utilization_up_pct": util_up_pct,
                    "utilization_pct": overall_util_pct,
                })

                # Upstream WAN link: Internet -> ONT
                links.append({
                    "id": f"link_internet_{oid}",
                    "source": "internet",
                    "target": oid,
                    "source_port": "Cloud",
                    "target_port": "PON",
                    "speed": speed_display,
                    "speed_down": speed_down,
                    "speed_up": speed_up,
                    "capacity_down_bps": cap_down_bps,
                    "capacity_up_bps": cap_up_bps,
                    "capacity_bps": max(cap_down_bps, cap_up_bps),
                    "tx_bps": rx_bps,  # Outbound from Internet is download
                    "rx_bps": tx_bps,  # Inbound to Internet is upload
                    "tx_bytes": rx_bytes,
                    "rx_bytes": tx_bytes,
                    "utilization_down_pct": util_down_pct,
                    "utilization_up_pct": util_up_pct,
                    "utilization_pct": overall_util_pct,
                    "type": "internet",
                    "status": ont_status,
                })

                # Downstream LAN link: ONT -> Parent Device
                if parent_ip:
                    links.append({
                        "id": f"link_{oid}_{parent_ip}",
                        "source": oid,
                        "target": parent_ip,
                        "source_port": "LAN",
                        "target_port": format_port_display(parent_port) if parent_port else "WAN",
                        "speed": speed_display,
                        "speed_down": speed_down,
                        "speed_up": speed_up,
                        "capacity_down_bps": cap_down_bps,
                        "capacity_up_bps": cap_up_bps,
                        "capacity_bps": max(cap_down_bps, cap_up_bps),
                        "tx_bps": tx_bps,
                        "rx_bps": rx_bps,
                        "tx_bytes": tx_bytes,
                        "rx_bytes": rx_bytes,
                        "utilization_down_pct": util_down_pct,
                        "utilization_up_pct": util_up_pct,
                        "utilization_pct": overall_util_pct,
                        "type": "uplink",
                        "status": ont_status,
                    })

        # Add Managed Switches and Routers
        for ip, sw in switches_by_ip.items():
            if str(sw.get("model", "")).lower() == "internet":
                continue
            if sw.get("topology_hub"):
                continue
            physical_host = physical_hosts.get(ip, {})
            sw_client_count = sum(1 for cl in client_links if cl.get("source") == ip)
            physical_name = physical_host.get("name")
            child_count = sum(
                1 for child in sw.get("child_devices", [])
                if not physical_name
                or _canonical_node_name(child.get("node")) == _canonical_node_name(physical_name)
            )
            nodes.append({
                "id": ip,
                "name": physical_name or sw["name"],
                "type": sw["role"],
                "role": sw["role"],
                "ip": physical_host.get("ip") or ip,
                "ips": physical_host.get("ips", []),
                "mac": physical_host.get("mac") or sw["mac"],
                "macs": physical_host.get("macs", []),
                "model": "Proxmox VE Host" if physical_host else sw["model"],
                "status": physical_host.get("status") or sw["status"],
                "ports": sw["ports"],
                "interfaces": physical_host.get("interfaces", []),
                "node": physical_name or "",
                "uptime": physical_host.get("uptime", 0),
                "cpu": physical_host.get("cpu", 0),
                "memory": physical_host.get("memory", 0),
                "max_memory": physical_host.get("max_memory", 0),
                "client_count": sw_client_count,
                "child_count": child_count,
                "cluster_name": sw.get("cluster_name", ""),
                "cluster_nodes": sw.get("cluster_nodes", []),
                "protocol": sw.get("protocol", ""),
            })

        # Add the remaining physical members discovered from each Proxmox cluster.
        for host_id, host in physical_hosts.items():
            if host_id in switches_by_ip:
                continue
            cluster_sw = switches_by_ip.get(host.get("cluster_ip", ""), {})
            child_count = sum(
                1 for child in cluster_sw.get("child_devices", [])
                if _canonical_node_name(child.get("node")) == _canonical_node_name(host.get("name"))
            )
            nodes.append({
                "id": host_id,
                "name": host.get("name", host_id),
                "type": NodeRole.VIRTUALISATION_HOST,
                "role": NodeRole.VIRTUALISATION_HOST,
                "device_type": "virtualisation_host",
                "ip": host.get("ip", ""),
                "ips": host.get("ips", []),
                "mac": host.get("mac", ""),
                "macs": host.get("macs", []),
                "model": "Proxmox VE Host",
                "status": host.get("status", "unknown"),
                "ports": [],
                "interfaces": host.get("interfaces", []),
                "node": host.get("name", ""),
                "uptime": host.get("uptime", 0),
                "cpu": host.get("cpu", 0),
                "memory": host.get("memory", 0),
                "max_memory": host.get("max_memory", 0),
                "client_count": 0,
                "child_count": child_count,
                "cluster_name": host.get("cluster_name", ""),
                "cluster_nodes": host.get("cluster_nodes", []),
                "topology_hub_id": host.get("topology_hub_id", ""),
                "protocol": cluster_sw.get("protocol", "proxmox"),
            })

        # Add normalized children discovered by managed infrastructure drivers.
        seen_child_ids = set()
        for host_ip, sw in switches_by_ip.items():
            for child in sw.get("child_devices", []):
                if not isinstance(child, dict) or not child.get("id"):
                    continue
                child_id = str(child["id"])
                if child_id in seen_child_ids:
                    continue
                seen_child_ids.add(child_id)
                kind = str(child.get("kind") or child.get("device_type") or NodeRole.VIRTUAL_MACHINE)
                child_node = str(child.get("node") or "")
                parent_host_id = physical_host_ids.get((host_ip, _canonical_node_name(child_node)))
                if not parent_host_id and child_node:
                    parent_host_id = physical_host_ids.get((host_ip, child_node.lower()))
                if not parent_host_id:
                    parent_host_id = cluster_default_host_ids.get(host_ip, host_ip)
                child_speed_tx = int(child.get("speed_tx_bps") or 0)
                child_speed_rx = int(child.get("speed_rx_bps") or 0)
                child_cum_tx = int(child.get("cum_tx") if child.get("cum_tx") is not None else (child.get("tx_bytes") or child.get("netout") or 0))
                child_cum_rx = int(child.get("cum_rx") if child.get("cum_rx") is not None else (child.get("rx_bytes") or child.get("netin") or 0))
                nodes.append({
                    "id": child_id,
                    "name": child.get("name", child_id),
                    "type": kind,
                    "role": kind,
                    "device_type": kind,
                    "status": child.get("status", "unknown"),
                    "state": child.get("state", "unknown"),
                    "native_id": str(child.get("native_id", "")),
                    "parent_host_id": parent_host_id,
                    "topology_hub_id": sw.get("device_id", host_ip),
                    "cluster_name": sw.get("cluster_name", ""),
                    "node": child.get("node", ""),
                    "macs": child.get("macs", []),
                    "ips": child.get("ips", []),
                    "interfaces": child.get("interfaces", []),
                    "template": child.get("template", False),
                    "uptime": child.get("uptime", 0),
                    "cpu": child.get("cpu", 0),
                    "memory": child.get("memory", 0),
                    "max_memory": child.get("max_memory", 0),
                    "disk": child.get("disk", 0),
                    "max_disk": child.get("max_disk", 0),
                    "tx_bps": child_speed_tx,
                    "rx_bps": child_speed_rx,
                    "speed_tx_bps": child_speed_tx,
                    "speed_rx_bps": child_speed_rx,
                    "tx_bytes": child_cum_tx,
                    "rx_bytes": child_cum_rx,
                    "cum_tx": child_cum_tx,
                    "cum_rx": child_cum_rx,
                    "netout": child_cum_tx,
                    "netin": child_cum_rx,
                })
                links.append({
                    "id": f"link_{parent_host_id}_{child_id}",
                    "source": parent_host_id,
                    "target": child_id,
                    "source_port": child.get("node", "Cluster"),
                    "target_port": str(child.get("native_id", "")),
                    "speed": "Virtual",
                    "tx_bps": child_speed_tx,
                    "rx_bps": child_speed_rx,
                    "tx_bytes": child_cum_tx,
                    "rx_bytes": child_cum_rx,
                    "capacity_bps": 0,
                    "utilization_pct": 0.0,
                    "type": "virtual_child",
                    "status": child.get("status", "unknown"),
                })

        # Add Access Points
        for ap_mac, ap in access_points.items():
            nodes.append({
                "id": ap["id"],
                "name": ap["name"],
                "type": NodeRole.ACCESS_POINT,
                "role": NodeRole.ACCESS_POINT,
                "mac": ap_mac,
                "ip": ap.get("ip", ""),
                "model": ap.get("model", "Access Point"),
                "vendor": ap.get("vendor", ""),
                "status": ap["status"],
            })
            if ap_mac in ap_connections:
                sw_ip, sw_port = ap_connections[ap_mac]
                speed = "1G"
                tx_bps = 0
                rx_bps = 0
                if sw_ip in switches_by_ip:
                    for p in switches_by_ip[sw_ip]["ports"]:
                        if ports_equal(p.get("port_id"), sw_port):
                            speed = p.get("speed", "1G")
                            t = p.get("telemetry", {})
                            tx_bps = t.get("tx_bps", 0)
                            rx_bps = t.get("rx_bps", 0)
                            break
                links.append({
                    "id": f"link_{sw_ip}_{ap['id']}",
                    "source": sw_ip,
                    "target": ap["id"],
                    "source_port": format_port_display(sw_port),
                    "target_port": "Uplink",
                    "speed": speed,
                    "tx_bps": tx_bps,
                    "rx_bps": rx_bps,
                    "capacity_bps": parse_speed_bps(speed),
                    "utilization_pct": round((max(tx_bps, rx_bps) / parse_speed_bps(speed)) * 100, 2) if parse_speed_bps(speed) > 0 else 0.0,
                    "type": "ap",
                    "status": "online",
                })

        # Add Unmanaged Switches
        for us_info in unmanaged_by_port.values():
            nodes.append({
                "id": us_info["id"],
                "name": us_info["name"],
                "type": NodeRole.UNMANAGED_SWITCH,
                "role": NodeRole.UNMANAGED_SWITCH,
                "status": us_info["status"],
                "parent_ip": us_info["parent_ip"],
                "parent_port": format_port_display(us_info["parent_port"]),
            })
            p_ip = us_info["parent_ip"]
            p_port = us_info["parent_port"]
            speed = "1G"
            phone_parent_id = resolve_phone_id(p_ip)
            if phone_parent_id:
                phone_node = phones[phone_parent_id]
                source_port_label = format_port_display(p_port) if p_port else phone_node.get("passthrough_port", "PC")
                links.append({
                    "id": f"link_{phone_parent_id}_{us_info['id']}",
                    "source": phone_parent_id,
                    "target": us_info["id"],
                    "source_port": source_port_label,
                    "target_port": "Uplink",
                    "speed": "1G",
                    "tx_bps": 0,
                    "rx_bps": 0,
                    "capacity_bps": 1_000_000_000,
                    "utilization_pct": 0.0,
                    "type": "infra",
                    "status": "online",
                })
            else:
                if p_ip in switches_by_ip:
                    for p in switches_by_ip[p_ip]["ports"]:
                        if ports_equal(p.get("port_id"), p_port):
                            speed = p.get("speed", "1G")
                            break
                links.append({
                    "id": f"link_{p_ip}_{us_info['id']}",
                    "source": p_ip,
                    "target": us_info["id"],
                    "source_port": format_port_display(p_port),
                    "target_port": "Uplink",
                    "speed": speed,
                    "tx_bps": 0,
                    "rx_bps": 0,
                    "capacity_bps": parse_speed_bps(speed),
                    "utilization_pct": 0.0,
                    "type": "infra",
                    "status": "online",
                })

        # Add Phone Nodes & Upstream Links
        for pid, phone in phones.items():
            p_ip = phone.get("parent_ip")
            p_port = phone.get("parent_port")
            pmac = normalize_mac(phone.get("mac", ""))
            disc_info = discovered_host_map.get(pmac, {})
            phone_ip = phone.get("ip") or disc_info.get("ip", "")
            phone_hostname = phone.get("hostname") or disc_info.get("hostname", "")

            nodes.append({
                "id": pid,
                "name": phone["name"],
                "type": NodeRole.PHONE,
                "role": NodeRole.PHONE,
                "device_type": "phone",
                "ip": phone_ip,
                "hostname": phone_hostname,
                "mac": phone.get("mac", ""),
                "parent_ip": phone.get("parent_ip", ""),
                "parent_port": format_port_display(phone.get("parent_port", "")),
                "is_mini_switch": phone.get("is_mini_switch", True),
                "passthrough": phone.get("passthrough", True),
                "passthrough_port": phone.get("passthrough_port", "PC"),
                "status": phone.get("status", "online"),
            })

            if p_ip and p_ip in switches_by_ip:
                speed = "1G"
                tx_bps = 0
                rx_bps = 0
                for p_info in switches_by_ip[p_ip]["ports"]:
                    if ports_equal(p_info.get("port"), p_port):
                        speed = p_info.get("speed", "1G")
                        tx_bps = p_info.get("speed_tx_bps", 0)
                        rx_bps = p_info.get("speed_rx_bps", 0)
                        break
                links.append({
                    "id": f"link_{p_ip}_{pid}",
                    "source": p_ip,
                    "target": pid,
                    "source_port": format_port_display(p_port),
                    "target_port": "LAN",
                    "speed": speed,
                    "tx_bps": tx_bps,
                    "rx_bps": rx_bps,
                    "capacity_bps": parse_speed_bps(speed),
                    "utilization_pct": 0.0,
                    "type": "uplink",
                    "status": phone.get("status", "online"),
                })

        # Add Infrastructure Devices
        for mac, dev in infra_by_mac.items():
            nodes.append({
                "id": mac,
                "name": dev["name"],
                "type": dev["type"],
                "mac": mac,
                "vendor": dev["vendor"],
                "status": "online" if mac in infra_connections or dev["type"] == "router" else "offline",
            })
            if mac in infra_connections:
                ip, port = infra_connections[mac]
                speed = "1G"
                if ip in switches_by_ip:
                    for p in switches_by_ip[ip]["ports"]:
                        if ports_equal(p.get("port_id"), port):
                            speed = p.get("speed", "1G")
                            break
                links.append({
                    "id": f"link_{ip}_{mac}",
                    "source": ip,
                    "target": mac,
                    "source_port": format_port_display(port),
                    "target_port": "",
                    "speed": speed,
                    "tx_bps": 0,
                    "rx_bps": 0,
                    "capacity_bps": parse_speed_bps(speed),
                    "utilization_pct": 0.0,
                    "type": "infra",
                    "status": "online",
                })

        # Add Clients
        for mac, dev in clients.items():
            nodes.append({
                "id": mac,
                "name": dev["name"],
                "type": NodeRole.CLIENT,
                "role": NodeRole.CLIENT,
                "device_type": dev.get("device_type", "laptop"),
                "mac": dev["mac"],
                "vendor": dev["vendor"],
                "status": dev["status"],
                "host": dev["host"],
                "hostname": dev.get("hostname", ""),
                "ip": dev.get("ip", ""),
                "last_seen_ip": dev["last_seen_ip"],
                "last_seen_port": dev["last_seen_port"],
                "last_seen_time": dev["last_seen_time"],
            })

        topology_hubs = []
        for hub_ip, sw in switches_by_ip.items():
            if not sw.get("topology_hub"):
                continue
            hub_id = sw.get("device_id", hub_ip)
            emitted_node_ids = [
                host_id for host_id, host in physical_hosts.items()
                if host.get("cluster_ip") == hub_ip
            ]
            emitted_node_ids.extend(
                str(child.get("id"))
                for child in sw.get("child_devices", [])
                if isinstance(child, dict) and child.get("id")
            )
            topology_hubs.append({
                "id": hub_id,
                "name": sw.get("name", hub_id),
                "protocol": sw.get("protocol", ""),
                "endpoint": hub_ip,
                "status": sw.get("status", "unknown"),
                "node_ids": emitted_node_ids,
            })

        return {
            "nodes": nodes,
            "links": links + client_links,
            "hubs": topology_hubs,
        }


_topology_service: Optional[TopologyService] = None


def get_topology_service() -> TopologyService:
    global _topology_service
    if _topology_service is None:
        _topology_service = TopologyService()
    return _topology_service
