import logging
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, Set, Tuple

from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.repositories.scanner_repo import ScannerRepository

logger = logging.getLogger("switch_dashboard.services.host_discovery")


class HostDiscoveryService:
    """Central service for aggregating client IP addresses and hostnames across

    all passive infrastructure sources (SNMP/Fritz!Box ARP tables, UniFi AP
    station tables, Proxmox hypervisor guest configs), the local Linux kernel
    neighbor cache (/proc/net/arp), scanner database, and asynchronous reverse
    DNS.
    """

    def __init__(self, db=None, cache_ttl: int = 3600, max_threads: int = 10):
        self.db = db or get_db()
        self.cache_ttl = cache_ttl
        self.max_threads = max_threads

        # Thread-safe in-memory cache for IP -> (hostname, timestamp)
        self._dns_cache: Dict[str, Tuple[Optional[str], float]] = {}
        self._dns_lock = threading.Lock()
        self._pending_ips: Set[str] = set()

        self._executor = ThreadPoolExecutor(
            max_workers=self.max_threads,
            thread_name_prefix="HostDiscoveryWorker",
        )

    @staticmethod
    def normalize_mac(mac: Any) -> str:
        clean = re.sub(r"[^0-9A-Fa-f]", "", str(mac or ""))
        return clean.upper() if len(clean) == 12 else ""

    @staticmethod
    def format_mac(mac: str) -> str:
        clean = re.sub(r"[^0-9A-Fa-f]", "", str(mac or "")).upper()
        if len(clean) == 12:
            return ":".join(clean[i : i + 2] for i in range(0, 12, 2))
        return mac

    def get_kernel_arp_table(self) -> Dict[str, str]:
        """Reads /proc/net/arp to map normalized clean MAC -> IP address.

        This is a zero-network-overhead local kernel memory read.
        """
        mac_to_ip: Dict[str, str] = {}
        try:
            with open("/proc/net/arp", "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    ip_addr = parts[0]
                    raw_mac = parts[3]
                    clean_mac = self.normalize_mac(raw_mac)
                    if clean_mac and clean_mac != "000000000000":
                        mac_to_ip[clean_mac] = ip_addr
        except Exception as e:
            logger.debug(f"Could not read /proc/net/arp: {e}")
        return mac_to_ip

    def get_scanner_hosts(self) -> Dict[str, Dict[str, str]]:
        """Loads known hosts from the scanner database table.

        Maps clean MAC -> {'ip': ip, 'hostname': hostname}.
        """
        results: Dict[str, Dict[str, str]] = {}
        try:
            repo = ScannerRepository(self.db)
            state = repo.load_state_from_db()
            for ip, host in state.items():
                mac = self.normalize_mac(host.get("mac_address", ""))
                if mac:
                    results[mac] = {
                        "ip": ip,
                        "hostname": host.get("hostname", "").strip(),
                    }
        except Exception as e:
            logger.debug(f"Could not load scanner hosts: {e}")
        return results

    def clean_hostname(self, name: Optional[str], ip: str = "") -> str:
        """Sanitizes resolved hostnames, removing trailing dots and filtering

        out generic gateway/IP placeholders.
        """
        if not name:
            return ""
        cleaned = name.strip().rstrip(".")
        # Filter out cases where the resolver just returned the IP itself or a gateway stub
        if not cleaned or cleaned == ip or cleaned.lower() in ("_gateway", "unknown", "localhost"):
            return ""
        return cleaned

    def resolve_hostname_sync(self, ip: str, timeout: float = 0.5) -> Optional[str]:
        """Performs a synchronous reverse DNS lookup with a short timeout."""
        if not ip:
            return None

        # Check cache first
        now = time.time()
        with self._dns_lock:
            if ip in self._dns_cache:
                cached_name, cached_at = self._dns_cache[ip]
                if now - cached_at < self.cache_ttl:
                    return cached_name

        # Perform lookup
        orig_timeout = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(timeout)
            host, _, _ = socket.gethostbyaddr(ip)
            cleaned = self.clean_hostname(host, ip)
        except Exception:
            cleaned = ""
        finally:
            socket.setdefaulttimeout(orig_timeout)

        # Store in cache
        with self._dns_lock:
            self._dns_cache[ip] = (cleaned if cleaned else None, now)

        return cleaned if cleaned else None

    def queue_hostname_resolution(self, ip: str, mac: str = ""):
        """Queues an IP for background reverse DNS resolution without blocking

        the calling thread.
        """
        if not ip:
            return
        now = time.time()
        with self._dns_lock:
            if ip in self._dns_cache:
                _, cached_at = self._dns_cache[ip]
                if now - cached_at < self.cache_ttl:
                    return
            if ip in self._pending_ips:
                return
            self._pending_ips.add(ip)

        def _worker():
            try:
                resolved = self.resolve_hostname_sync(ip, timeout=0.8)
                if resolved and mac:
                    # Update discovered_clients in database
                    from switch_dashboard.storage.repositories.device_repo import DeviceRepository

                    DeviceRepository(self.db).update_discovered_client_meta(
                        mac=mac, hostname=resolved, ip=ip
                    )
            except Exception as e:
                logger.debug(f"Background rDNS failed for {ip}: {e}")
            finally:
                with self._dns_lock:
                    self._pending_ips.discard(ip)

        self._executor.submit(_worker)

    def resolve_all_clients(
        self,
        switches_by_ip: Dict[str, Any],
        infra_ips: Optional[Set[str]] = None,
    ) -> Dict[str, Dict[str, str]]:
        """Aggregates all known IP addresses and hostnames for every client MAC.

        Draws from:
        1. Router / Switch polled ARP & MAC tables (e.g. SNMP RFC 1213, Fritz!Box TR-064)
        2. Hypervisor guest devices (Proxmox VMs & LXCs)
        3. UniFi AP station tables
        4. Host OS kernel ARP table (/proc/net/arp)
        5. Network scanner database (hosts table)
        6. In-memory reverse DNS cache

        Returns a dictionary mapping clean MAC -> {'ip': ip, 'hostname': hostname}.
        """
        infra_ips = infra_ips or set()
        resolved: Dict[str, Dict[str, str]] = {}

        def _update(mac: str, ip: str = "", host: str = ""):
            clean_m = self.normalize_mac(mac)
            if not clean_m:
                return
            if clean_m not in resolved:
                resolved[clean_m] = {"ip": "", "hostname": ""}

            clean_ip = str(ip or "").split("/")[0].strip()
            # Infrastructure switch/router IPs should never be assigned to clients
            if clean_ip and clean_ip not in infra_ips:
                if not resolved[clean_m]["ip"]:
                    resolved[clean_m]["ip"] = clean_ip

            clean_h = self.clean_hostname(host, clean_ip)
            if clean_h and not resolved[clean_m]["hostname"]:
                resolved[clean_m]["hostname"] = clean_h

        # 1. Polled switch & router data
        for sw_ip, sw_data in switches_by_ip.items():
            # A. Switch / Router MAC & ARP table
            for entry in sw_data.get("mac_table", []):
                e_mac = entry.get("mac", "")
                e_ip = entry.get("ip", "")
                e_host = entry.get("hostname", "")
                _update(e_mac, e_ip, e_host)

            # B. Proxmox / Hypervisor child devices
            for child in sw_data.get("child_devices", []):
                c_name = child.get("name", "")
                for m in child.get("macs", []):
                    # Check interfaces for specific IP mapping
                    matched_ip = ""
                    for iface in child.get("interfaces", []):
                        if self.normalize_mac(iface.get("mac", "")) == self.normalize_mac(m):
                            matched_ip = iface.get("ip", "")
                            break
                    if not matched_ip and child.get("ips"):
                        matched_ip = child["ips"][0]
                    _update(m, matched_ip, c_name)

        # 2. Host kernel ARP cache (/proc/net/arp)
        kernel_arp = self.get_kernel_arp_table()
        for k_mac, k_ip in kernel_arp.items():
            _update(k_mac, ip=k_ip)

        # 3. Scanner database hosts
        scanner_hosts = self.get_scanner_hosts()
        for s_mac, s_info in scanner_hosts.items():
            _update(s_mac, ip=s_info.get("ip", ""), host=s_info.get("hostname", ""))

        # 4. Check cached reverse DNS or queue background resolution
        now = time.time()
        for mac, info in resolved.items():
            ip_val = info.get("ip", "")
            if ip_val and not info.get("hostname"):
                # Check in-memory DNS cache
                with self._dns_lock:
                    if ip_val in self._dns_cache:
                        cached_h, cached_at = self._dns_cache[ip_val]
                        if now - cached_at < self.cache_ttl and cached_h:
                            info["hostname"] = cached_h
                # If still not found, queue background lookup
                if not info.get("hostname"):
                    self.queue_hostname_resolution(ip_val, mac=mac)

        return resolved


_default_host_discovery: Optional[HostDiscoveryService] = None


def get_host_discovery_service() -> HostDiscoveryService:
    global _default_host_discovery
    if _default_host_discovery is None:
        _default_host_discovery = HostDiscoveryService()
    return _default_host_discovery
