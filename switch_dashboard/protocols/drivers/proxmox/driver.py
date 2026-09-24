import asyncio
import logging
import re
import ssl
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import voluptuous as vol

from switch_dashboard.protocols.base import BaseProtocol, Capability
from switch_dashboard.protocols.registry import register_protocol

logger = logging.getLogger(__name__)


@register_protocol(
    name="proxmox",
    display_name="Proxmox VE (API Token)",
    supported_models=["proxmox", "proxmox_ve", "pve"],
)
class ProxmoxProtocol(BaseProtocol):
    """Cluster-wide Proxmox VE inventory driver using the asynchronous REST API."""

    name = "proxmox"
    display_name = "Proxmox VE (API Token)"
    capabilities = {Capability.METRICS, Capability.CHILD_DEVICES}
    config_schema = vol.Schema({
        vol.Required("api_user", description="API Token User (for example dashboard@pve)"): str,
        vol.Required("token_id", description="API Token Name"): str,
        vol.Required("token_secret", description="API Token Secret"): str,
        vol.Optional("api_port", default=8006, description="Proxmox HTTPS API Port"): vol.Coerce(int),
        vol.Optional("api_timeout", default=15, description="API Request Timeout (seconds)"): vol.Coerce(int),
        vol.Optional("verify_ssl", default=True, description="Verify the Proxmox TLS Certificate"): bool,
        vol.Optional("ca_bundle", default="", description="Custom CA Certificate Bundle Path"): str,
        vol.Optional("max_concurrency", default=10, description="Concurrent Guest Configuration Requests"): vol.Coerce(int),
    })

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_user = config.get("api_user", "")
        self.token_id = config.get("token_id", "")
        self.token_secret = config.get("token_secret", "")
        self.api_port = int(config.get("api_port", 8006))
        self.api_timeout = int(config.get("api_timeout", 15))
        self.verify_ssl = config.get("verify_ssl", True)
        self.ca_bundle = config.get("ca_bundle", "")
        self.max_concurrency = max(1, int(config.get("max_concurrency", 10)))
        self.device_id = str(config.get("id") or self.ip)
        self._cached_data: Dict[str, Any] = {}
        self._cache_time = 0.0
        self._cache_seconds = 5.0

    @property
    def base_url(self) -> str:
        return f"https://{self.ip}:{self.api_port}/api2/json"

    def _ssl_context(self):
        if not self.verify_ssl:
            return False
        if self.ca_bundle:
            return ssl.create_default_context(cafile=self.ca_bundle)
        return ssl.create_default_context()

    def _headers(self) -> Dict[str, str]:
        token = f"{self.api_user}!{self.token_id}={self.token_secret}"
        return {"Authorization": f"PVEAPIToken={token}"}

    async def _request_json(
        self,
        session: aiohttp.ClientSession,
        path: str,
        params: Optional[Dict[str, str]] = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        async with session.get(url, params=params) as response:
            if response.status >= 400:
                detail = (await response.text()).strip()
                raise RuntimeError(f"Proxmox API {path} returned HTTP {response.status}: {detail[:200]}")
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict) or "data" not in payload:
            raise ValueError(f"Proxmox API {path} returned an invalid response")
        return payload["data"]

    def _session(self) -> aiohttp.ClientSession:
        timeout = aiohttp.ClientTimeout(total=self.api_timeout)
        connector = aiohttp.TCPConnector(ssl=self._ssl_context())
        return aiohttp.ClientSession(headers=self._headers(), timeout=timeout, connector=connector)

    def test_connection(self) -> Tuple[bool, str]:
        try:
            version, cluster_status = asyncio.run(self._test_connection_async())
            cluster_name = self._cluster_name(cluster_status)
            release = version.get("release") or version.get("version") or "unknown"
            return True, f"Connected to Proxmox VE {release} ({cluster_name})"
        except Exception as exc:
            return False, f"Proxmox API connection failed: {exc}"

    async def _test_connection_async(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        async with self._session() as session:
            version, cluster_status = await asyncio.gather(
                self._request_json(session, "version"),
                self._request_json(session, "cluster/status"),
            )
        return version or {}, cluster_status or []

    def scrape(self) -> Dict[str, Any]:
        now = time.time()
        if self._cached_data and now - self._cache_time < self._cache_seconds:
            return self._cached_data
        try:
            self._cached_data = asyncio.run(self._scrape_async())
        except Exception as exc:
            logger.warning("Proxmox API scrape failed for %s: %s", self.ip, exc)
            self._cached_data = self._offline_result(str(exc))
        self._cache_time = now
        return self._cached_data

    async def _scrape_async(self) -> Dict[str, Any]:
        async with self._session() as session:
            version, cluster_status, nodes, resources = await asyncio.gather(
                self._request_json(session, "version"),
                self._request_json(session, "cluster/status"),
                self._request_json(session, "nodes"),
                self._request_json(session, "cluster/resources", params={"type": "vm"}),
            )
            children, node_networks = await asyncio.gather(
                self._load_children(session, resources or []),
                self._load_node_networks(session, nodes or []),
            )

        cluster_name = self._cluster_name(cluster_status or [])
        online_nodes = sum(1 for node in nodes or [] if node.get("status") == "online")
        uptime = max((int(node.get("uptime") or 0) for node in nodes or []), default=0)
        release = str((version or {}).get("release") or (version or {}).get("version") or "")
        return {
            "name": self.config.get("name") or cluster_name,
            "ip": self.ip,
            "model": "Proxmox VE Cluster",
            "firmware": release,
            "uptime": self._format_uptime(uptime),
            "mac": self.config.get("mac", ""),
            "role": "virtualisation_host",
            "device_type": "virtualisation_host",
            "topology_hub": True,
            "status": "online",
            "ports": [],
            "mac_table": [],
            "neighbors": [],
            "child_devices": children,
            "cluster_name": cluster_name,
            "cluster_nodes": self._normalize_nodes(
                nodes or [], cluster_status or [], node_networks
            ),
            "node_count": len(nodes or []),
            "online_node_count": online_nodes,
            "guest_count": len(children),
            "timestamp": time.time(),
        }

    async def _load_node_networks(
        self,
        session: aiohttp.ClientSession,
        nodes: List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def load(node: Dict[str, Any]):
            node_name = str(node.get("node") or "")
            if not node_name:
                return node_name, []
            try:
                async with semaphore:
                    network = await self._request_json(session, f"nodes/{node_name}/network")
                return node_name, network if isinstance(network, list) else []
            except Exception as exc:
                logger.debug("Could not load Proxmox node network %s: %s", node_name, exc)
                return node_name, []

        results = await asyncio.gather(*(load(node) for node in nodes))
        return dict(results)

    async def _load_children(
        self,
        session: aiohttp.ClientSession,
        resources: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def load(resource: Dict[str, Any]) -> Dict[str, Any]:
            guest_type = str(resource.get("type") or "")
            node = str(resource.get("node") or "")
            vmid = str(resource.get("vmid") or "")
            config: Dict[str, Any] = {}
            config_error = ""
            if guest_type in {"qemu", "lxc"} and node and vmid:
                try:
                    async with semaphore:
                        config = await self._request_json(
                            session, f"nodes/{node}/{guest_type}/{vmid}/config"
                        ) or {}
                except Exception as exc:
                    config_error = str(exc)
                    logger.debug("Could not load Proxmox guest config %s/%s: %s", guest_type, vmid, exc)
            return self._normalize_child(resource, config, config_error)

        children = await asyncio.gather(*(load(resource) for resource in resources))
        return sorted(children, key=lambda item: (self._vmid_sort_key(item["native_id"]), item["kind"]))

    def _normalize_child(
        self,
        resource: Dict[str, Any],
        config: Dict[str, Any],
        config_error: str = "",
    ) -> Dict[str, Any]:
        guest_type = str(resource.get("type") or "qemu").lower()
        kind = "lxc_container" if guest_type == "lxc" else "virtual_machine"
        vmid = str(resource.get("vmid") or config.get("vmid") or "")
        interfaces = self._parse_interfaces(config, guest_type)
        macs = []
        ips = []
        for interface in interfaces:
            if interface.get("mac") and interface["mac"] not in macs:
                macs.append(interface["mac"])
            if interface.get("ip") and interface["ip"] not in {"dhcp", "manual"}:
                ips.append(interface["ip"])
        raw_status = str(resource.get("status") or "unknown").lower()
        status = "online" if raw_status == "running" else ("stopped" if raw_status == "stopped" else raw_status)
        netin = int(resource.get("netin") or 0)
        netout = int(resource.get("netout") or 0)
        for interface in interfaces:
            interface.setdefault("rx_bytes", netin)
            interface.setdefault("tx_bytes", netout)
            interface.setdefault("netin", netin)
            interface.setdefault("netout", netout)
        child = {
            "id": f"proxmox:{self.device_id}:{guest_type}:{vmid}",
            "native_id": vmid,
            "name": str(resource.get("name") or config.get("name") or f"{guest_type.upper()} {vmid}"),
            "kind": kind,
            "device_type": kind,
            "status": status,
            "state": raw_status,
            "node": str(resource.get("node") or ""),
            "macs": macs,
            "ips": ips,
            "interfaces": interfaces,
            "template": bool(resource.get("template") or config.get("template")),
            "uptime": int(resource.get("uptime") or 0),
            "cpu": float(resource.get("cpu") or 0),
            "memory": int(resource.get("mem") or 0),
            "max_memory": int(resource.get("maxmem") or 0),
            "disk": int(resource.get("disk") or 0),
            "max_disk": int(resource.get("maxdisk") or 0),
            "netin": netin,
            "netout": netout,
            "rx_bytes": netin,
            "tx_bytes": netout,
            "cum_rx": netin,
            "cum_tx": netout,
        }
        if config_error:
            child["config_error"] = config_error
        return child

    @classmethod
    def _parse_interfaces(cls, config: Dict[str, Any], guest_type: str) -> List[Dict[str, Any]]:
        interfaces = []
        for key, value in sorted(config.items()):
            if not re.fullmatch(r"net\d+", str(key)) or not isinstance(value, str):
                continue
            parts = [part.strip() for part in value.split(",") if part.strip()]
            values: Dict[str, str] = {}
            for part in parts:
                if "=" in part:
                    name, item_value = part.split("=", 1)
                    values[name.strip().lower()] = item_value.strip()

            if guest_type == "lxc":
                name = values.get("name", key)
                mac = values.get("hwaddr", "")
            else:
                model_names = ("virtio", "e1000", "e1000e", "vmxnet3", "rtl8139", "ne2k_pci")
                model = next((item for item in model_names if item in values), "")
                name = key
                mac = values.get(model, "") if model else ""
                if not mac:
                    mac = cls._extract_mac_from_qemu_values(values, value)

            normalized_mac = cls._normalize_mac(mac)
            interface = {
                "name": name,
                "config_key": str(key),
                "mac": normalized_mac,
                "bridge": values.get("bridge", ""),
                "vlan": values.get("tag", ""),
                "firewall": values.get("firewall", "0") in {"1", "true", "yes"},
            }
            ip_value = values.get("ip", "")
            if ip_value:
                interface["ip"] = ip_value
            interfaces.append(interface)
        return interfaces

    @classmethod
    def _extract_mac_from_qemu_values(cls, values: Dict[str, str], raw_value: str) -> str:
        ignored_keys = {
            "bridge", "tag", "firewall", "link_down", "rate", "queues", "mtu",
            "trunks", "name", "type", "ip", "gw", "gw6", "ip6", "id",
        }
        for key, value in values.items():
            if key in ignored_keys:
                continue
            normalized = cls._normalize_mac(value)
            if normalized:
                return normalized

        match = cls._mac_regex.search(raw_value or "")
        if match:
            return cls._normalize_mac(match.group(1))
        return ""

    def scrape_mac_table(self) -> List[Dict[str, Any]]:
        return []

    def scrape_child_devices(self) -> List[Dict[str, Any]]:
        return self.scrape().get("child_devices", [])

    def _offline_result(self, error: str) -> Dict[str, Any]:
        return {
            "name": self.config.get("name", self.ip),
            "ip": self.ip,
            "model": "Proxmox VE Cluster",
            "firmware": "",
            "uptime": "",
            "mac": self.config.get("mac", ""),
            "role": "virtualisation_host",
            "device_type": "virtualisation_host",
            "topology_hub": True,
            "status": "offline",
            "error": error,
            "ports": [],
            "mac_table": [],
            "child_devices": [],
            "timestamp": time.time(),
        }

    @staticmethod
    def _cluster_name(cluster_status: List[Dict[str, Any]]) -> str:
        cluster = next((item for item in cluster_status if item.get("type") == "cluster"), None)
        if cluster:
            return str(cluster.get("name") or "Proxmox Cluster")
        node = next((item for item in cluster_status if item.get("type") == "node"), None)
        return str((node.get("name") if node else "") or "Proxmox Cluster")

    def _normalize_nodes(
        self,
        nodes: List[Dict[str, Any]],
        cluster_status: List[Dict[str, Any]],
        node_networks: Dict[str, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        status_by_name = {
            str(item.get("name") or ""): item
            for item in cluster_status
            if item.get("type") == "node" and item.get("name")
        }
        normalized = []
        for node in nodes:
            name = str(node.get("node") or "")
            status = status_by_name.get(name, {})
            interfaces = []
            macs = []
            ips = []
            for interface in node_networks.get(name, []):
                if not isinstance(interface, dict):
                    continue
                mac = self._extract_interface_mac(interface)
                interface_ips = []
                for key in ("cidr", "address", "cidr6", "address6"):
                    value = str(interface.get(key) or "")
                    if value and not self._normalize_mac(value) and value not in interface_ips:
                        interface_ips.append(value)
                        if value not in ips:
                            ips.append(value)
                if mac and mac not in macs:
                    macs.append(mac)
                interfaces.append({
                    "name": str(interface.get("iface") or interface.get("name") or ""),
                    "type": str(interface.get("type") or ""),
                    "mac": mac,
                    "ips": interface_ips,
                    "bridge_ports": str(interface.get("bridge_ports") or ""),
                    "active": bool(interface.get("active", interface.get("exists", False))),
                })

            ordered_macs = self._order_host_macs(interfaces, macs)

            node_ip = str(status.get("ip") or "")
            if node_ip and node_ip not in ips:
                ips.insert(0, node_ip)
            normalized.append({
                "id": f"proxmox:{self.device_id}:node:{name}",
                "name": name,
                "status": str(node.get("status") or status.get("online") or "unknown"),
                "local": bool(status.get("local")),
                "ip": node_ip,
                "ips": ips,
                "mac": ordered_macs[0] if ordered_macs else "",
                "macs": ordered_macs,
                "interfaces": interfaces,
                "uptime": int(node.get("uptime") or 0),
                "cpu": float(node.get("cpu") or 0),
                "memory": int(node.get("mem") or 0),
                "max_memory": int(node.get("maxmem") or 0),
            })
        return normalized

    @classmethod
    def _extract_interface_mac(cls, interface: Dict[str, Any]) -> str:
        for key in ("hwaddress", "hwaddr", "macaddr", "mac", "mac_address"):
            val = interface.get(key)
            if val:
                norm = cls._normalize_mac(val)
                if norm:
                    return norm

        # Predictable network interface names (e.g. enx0002c910ee64) in altnames or name
        altnames = interface.get("altnames")
        if isinstance(altnames, list):
            for item in altnames:
                item_str = str(item or "").strip()
                if item_str.lower().startswith("enx") and len(item_str) == 15:
                    norm = cls._normalize_mac(item_str[3:])
                    if norm:
                        return norm
                extracted = cls._extract_mac_from_text(item_str)
                if extracted:
                    return extracted
        elif isinstance(altnames, str) and altnames:
            extracted = cls._extract_mac_from_text(altnames)
            if extracted:
                return extracted

        iface_name = str(interface.get("iface") or interface.get("name") or "").strip()
        if iface_name.lower().startswith("enx") and len(iface_name) == 15:
            norm = cls._normalize_mac(iface_name[3:])
            if norm:
                return norm

        raw_options = " ".join(
            str(interface.get(key) or "")
            for key in ("options", "ovs_options", "comments")
        )
        extracted = cls._extract_mac_from_text(raw_options)
        if extracted:
            return extracted

        raw_address = str(interface.get("address") or "")
        return cls._normalize_mac(raw_address)

    @classmethod
    def _extract_mac_from_text(cls, raw_value: str) -> str:
        match = cls._mac_regex.search(raw_value or "")
        if match:
            return cls._normalize_mac(match.group(1))
        return ""

    @classmethod
    def _order_host_macs(cls, interfaces: List[Dict[str, Any]], macs: List[str]) -> List[str]:
        mac_to_rank: Dict[str, Tuple[int, int, str]] = {}
        for interface in interfaces:
            mac = cls._normalize_mac(interface.get("mac", ""))
            if not mac:
                continue
            iface_type = str(interface.get("type") or "").lower()
            iface_name = str(interface.get("name") or "").lower()
            is_active = 0 if interface.get("active") else 1
            if iface_type in {"eth", "bond", "linux_bond"}:
                kind_rank = 0
            elif iface_type in {"bridge", "ovs_bridge", "openvswitch"} or iface_name.startswith("vmbr"):
                kind_rank = 2
            else:
                kind_rank = 1
            rank = (kind_rank, is_active, iface_name)
            previous = mac_to_rank.get(mac)
            if previous is None or rank < previous:
                mac_to_rank[mac] = rank

        ordered = sorted(macs, key=lambda item: mac_to_rank.get(item, (9, 9, item)))
        deduped = []
        for mac in ordered:
            if mac and mac not in deduped:
                deduped.append(mac)
        return deduped

    @staticmethod
    def _normalize_mac(value: Any) -> str:
        clean = re.sub(r"[^0-9A-Fa-f]", "", str(value or ""))
        if len(clean) != 12:
            return ""
        return ":".join(clean[index:index + 2] for index in range(0, 12, 2)).upper()

    @staticmethod
    def _format_uptime(seconds: int) -> str:
        days, remainder = divmod(max(0, seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{days} days, {hours:02d}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def _vmid_sort_key(value: str):
        try:
            return 0, int(value)
        except (TypeError, ValueError):
            return 1, str(value)

    _mac_regex = re.compile(r"(?:^|[^0-9A-Fa-f])([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?:[^0-9A-Fa-f]|$)")
