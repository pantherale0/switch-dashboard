import logging
import threading
import time
from typing import Any, Dict, List, Optional

from switch_dashboard.config import load_config
from switch_dashboard.services.clients.classifier import DeviceClassifier
from switch_dashboard.services.clients.metrics import ClientMetricsCalculator
from switch_dashboard.services.clients.mobility import MobilityTracker
from switch_dashboard.services.clients.models import (
    clean_mac_str,
    format_bps,
    format_bytes,
    get_subnet_cidr,
    signal_dbm_to_percent,
)
from switch_dashboard.services.clients.pipeline import ClientHandler
from switch_dashboard.services.clients.resolver import EdgeResolver
from switch_dashboard.services.vendor_service import get_vendor_service
from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.repositories.device_repo import DeviceRepository

logger = logging.getLogger("switch_dashboard.services.clients.service")


class ClientMonitorService:
    """Active network client monitoring service coordinating client lifecycle,
    mobility tracking, post-polling ingestion, Fing-style classifications,
    and UI dossiers.
    """

    def __init__(
        self,
        db=None,
        device_repo: Optional[DeviceRepository] = None,
        vendor_service=None,
        min_roam_debounce_seconds: float = 15.0,
    ):
        self.db = db or get_db()
        self.device_repo = device_repo or DeviceRepository(self.db)
        self.vendor_service = vendor_service or get_vendor_service()
        self._lock = threading.RLock()
        self._min_roam_debounce_seconds = min_roam_debounce_seconds

        # Modular components
        self.classifier = DeviceClassifier()
        self.resolver = EdgeResolver()
        self.mobility = MobilityTracker(
            device_repo=self.device_repo,
            min_roam_debounce_seconds=min_roam_debounce_seconds,
        )
        self.metrics = ClientMetricsCalculator()
        self.handler = ClientHandler(
            device_repo=self.device_repo,
            vendor_service=self.vendor_service,
            mobility_tracker=self.mobility,
            metrics_calculator=self.metrics,
            edge_resolver=self.resolver,
            classifier=self.classifier,
        )

        try:
            self.device_repo.cleanup_spurious_roamed_events()
        except Exception as e:
            logger.debug(f"Initial spurious roamed events cleanup skipped: {e}")

        logger.info("ClientMonitorService initialized with modular subpackage architecture.")

    @property
    def _last_footprint_hashes(self) -> Dict[str, str]:
        return self.handler._last_footprint_hashes

    @property
    def _last_locations(self) -> Dict[str, Any]:
        return self.mobility._last_locations

    @property
    def _live_speeds(self) -> Dict[str, Any]:
        return self.metrics._live_speeds

    @property
    def _prev_counters(self) -> Dict[str, Any]:
        return self.metrics._prev_counters

    def classify_device(
        self,
        vendor: str = "",
        hostname: str = "",
        custom_name: str = "",
        device_type: str = "",
        mac: str = "",
    ) -> Dict[str, str]:
        """Classifies a client into Fing-style device categories, icons, and brand/model."""
        return self.classifier.classify(
            vendor=vendor,
            hostname=hostname,
            custom_name=custom_name,
            device_type=device_type,
            mac=mac,
        )

    def process_polled_cycle(
        self,
        results: Dict[str, Dict[str, Any]],
        now: Optional[float] = None,
    ):
        """Processes a completed switch/AP polling cycle to ingest telemetry,
        track client mobility/roaming, update IP address bindings, and compute bandwidth.
        """
        with self._lock:
            self.handler.process_cycle(results, now=now)

    def get_subnets_summary(self) -> List[Dict[str, Any]]:
        """Returns all detected subnets grouped by CIDR with online and total counts."""
        all_clients = self.device_repo.get_all_discovered_clients()
        subnets: Dict[str, Dict[str, Any]] = {}
        now = time.time()

        for c in all_clients.values():
            ip = c.get("ip") or ""
            cidr = get_subnet_cidr(ip) if ip else "Unassigned"
            status = c.get("status", "online")
            last_seen = float(c.get("last_seen") or 0)
            is_online = (status == "online") and (now - last_seen < 600)

            if cidr not in subnets:
                subnets[cidr] = {
                    "cidr": cidr,
                    "name": cidr,
                    "online_count": 0,
                    "total_count": 0,
                    "last_active": last_seen,
                }

            subnets[cidr]["total_count"] += 1
            if is_online:
                subnets[cidr]["online_count"] += 1
            if last_seen > subnets[cidr]["last_active"]:
                subnets[cidr]["last_active"] = last_seen

        sorted_subnets = sorted(
            subnets.values(),
            key=lambda x: (x["online_count"], x["total_count"]),
            reverse=True,
        )

        for s in sorted_subnets:
            diff = now - s["last_active"]
            if diff < 60:
                s["last_seen_str"] = "Just now"
            elif diff < 3600:
                s["last_seen_str"] = f"{int(diff // 60)}m ago"
            elif diff < 86400:
                s["last_seen_str"] = time.strftime("%H:%M", time.localtime(s["last_active"]))
            else:
                s["last_seen_str"] = time.strftime("%b %d", time.localtime(s["last_active"]))

        logger.debug(f"Computed subnets summary: found {len(sorted_subnets)} subnets across {len(all_clients)} clients")
        return sorted_subnets

    def get_monitoring_summary(self, subnet_filter: Optional[str] = None) -> Dict[str, Any]:
        """Calculates Fing hero summary metrics: Up / Discovered ratio, most
        popular device type, most popular vendor, and live network client throughput.
        """
        all_clients = self.device_repo.get_all_discovered_clients()
        now = time.time()

        filtered = []
        for c in all_clients.values():
            ip = c.get("ip") or ""
            if subnet_filter and subnet_filter not in ("all", ""):
                cidr = get_subnet_cidr(ip) if ip else "Unassigned"
                if cidr != subnet_filter:
                    continue
            filtered.append(c)

        total_discovered = len(filtered)
        up_count = 0
        device_types_counter: Dict[str, int] = {}
        vendors_counter: Dict[str, int] = {}
        total_down_bps = 0
        total_up_bps = 0

        with self._lock:
            for c in filtered:
                mac = c.get("mac") or ""
                last_seen = float(c.get("last_seen") or 0)
                is_online = (c.get("status") == "online") and (now - last_seen < 600)
                if is_online:
                    up_count += 1

                # Classify
                classification = self.classify_device(
                    vendor=c.get("vendor", ""),
                    hostname=c.get("hostname", ""),
                    custom_name=c.get("custom_name", ""),
                    device_type=c.get("device_type", "client"),
                    mac=mac,
                )
                dtype = classification["category_label"]
                device_types_counter[dtype] = device_types_counter.get(dtype, 0) + 1

                brand = classification["brand"]
                if brand and brand != "Unknown":
                    vendors_counter[brand] = vendors_counter.get(brand, 0) + 1

                # Speeds
                live = self._live_speeds.get(mac)
                if live and is_online:
                    if hasattr(live, "speed_rx_bps"):
                        total_down_bps += int(live.speed_rx_bps or 0)
                        total_up_bps += int(live.speed_tx_bps or 0)
                    elif isinstance(live, dict):
                        total_down_bps += int(live.get("speed_rx_bps", 0))
                        total_up_bps += int(live.get("speed_tx_bps", 0))

        # Most popular type
        sorted_types = sorted(device_types_counter.items(), key=lambda x: x[1], reverse=True)
        if sorted_types:
            top_type_name, top_type_count = sorted_types[0]
            popular_type_str = top_type_name
            popular_type_subtitle = f"Most popular of {len(device_types_counter)} types ({top_type_count})"
        else:
            popular_type_str = "None"
            popular_type_subtitle = "No devices detected"

        # Most popular vendor
        sorted_vendors = sorted(vendors_counter.items(), key=lambda x: x[1], reverse=True)
        if sorted_vendors:
            top_vendor_name, top_vendor_count = sorted_vendors[0]
            popular_vendor_str = top_vendor_name
            popular_vendor_subtitle = f"Most popular of {len(vendors_counter)} vendors ({top_vendor_count})"
        else:
            popular_vendor_str = "None"
            popular_vendor_subtitle = "No vendors detected"

        summary_data = {
            "up_count": up_count,
            "total_discovered": total_discovered,
            "offline_count": total_discovered - up_count,
            "ratio_str": f"{up_count} / {total_discovered}",
            "popular_type": popular_type_str,
            "popular_type_subtitle": popular_type_subtitle,
            "popular_vendor": popular_vendor_str,
            "popular_vendor_subtitle": popular_vendor_subtitle,
            "total_down_bps": total_down_bps,
            "total_up_bps": total_up_bps,
            "total_down_formatted": format_bps(total_down_bps),
            "total_up_formatted": format_bps(total_up_bps),
            "last_discovered": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "last_changed": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        }
        logger.debug(
            f"Computed Fing monitoring summary (filter='{subnet_filter}'): "
            f"{up_count}/{total_discovered} online, {total_down_bps} bps down, {total_up_bps} bps up"
        )
        return summary_data

    def get_client_list(
        self,
        subnet_filter: Optional[str] = None,
        category_filter: Optional[str] = None,
        status_filter: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Returns the full enriched list of clients matching filters for the Fing table."""
        all_clients = self.device_repo.get_all_discovered_clients()
        now = time.time()
        result = []
        search_lower = (search or "").lower().strip()

        with self._lock:
            for c in all_clients.values():
                mac = c.get("mac") or ""
                ip = c.get("ip") or ""
                hostname = c.get("hostname") or ""
                custom_name = c.get("custom_name") or ""
                vendor = c.get("vendor") or ""
                switch_ip = c.get("switch_ip") or ""
                port = c.get("port") or ""
                vlan = c.get("vlan") or "1"
                ssid = c.get("ssid") or ""
                raw_sig = c.get("signal_dbm")
                last_seen = float(c.get("last_seen") or 0)
                first_seen = float(c.get("first_seen") or 0)

                # Online status check (active within 10 min)
                is_online = (c.get("status") == "online") and (now - last_seen < 600)
                status_str = "online" if is_online else "offline"

                # Subnet filter
                cidr = get_subnet_cidr(ip) if ip else "Unassigned"
                if subnet_filter and subnet_filter not in ("all", "") and cidr != subnet_filter:
                    continue

                # Status filter
                if status_filter:
                    if status_filter == "online" and not is_online:
                        continue
                    if status_filter == "offline" and is_online:
                        continue

                # Classification
                cls = self.classify_device(
                    vendor=vendor,
                    hostname=hostname,
                    custom_name=custom_name,
                    device_type=c.get("device_type", "client"),
                    mac=mac,
                )

                # Category filter
                if category_filter and category_filter not in ("all", ""):
                    if cls["category"] != category_filter:
                        continue

                # Search filter
                if search_lower:
                    search_space = f"{mac} {ip} {hostname} {custom_name} {vendor} {cls['brand']} {cls['model']} {port}".lower()
                    if search_lower not in search_space:
                        continue

                # Signal & Wireless
                signal_percent = signal_dbm_to_percent(raw_sig)
                is_wireless = bool(ssid or "wlan" in port.lower() or "ath" in port.lower() or "ra" in port.lower())

                # Live speeds
                live = self._live_speeds.get(mac)
                if live and hasattr(live, "speed_tx_bps"):
                    speed_tx = int(live.speed_tx_bps or 0) if is_online else 0
                    speed_rx = int(live.speed_rx_bps or 0) if is_online else 0
                    cum_tx = int(live.cum_tx or 0)
                    cum_rx = int(live.cum_rx or 0)
                elif isinstance(live, dict):
                    speed_tx = int(live.get("speed_tx_bps", 0)) if is_online else 0
                    speed_rx = int(live.get("speed_rx_bps", 0)) if is_online else 0
                    cum_tx = int(live.get("cum_tx", 0))
                    cum_rx = int(live.get("cum_rx", 0))
                else:
                    speed_tx = 0
                    speed_rx = 0
                    cum_tx = 0
                    cum_rx = 0

                # Display Name (Prefer Custom > Hostname > Model > IP > MAC)
                display_name = custom_name or hostname or cls["model"] or (f"Client {ip}" if ip else f"Client {mac[-8:]}")

                # Format Brand & Model string like Fing
                brand = cls["brand"]
                model = cls["model"]
                if brand and model and brand.lower() != model.lower():
                    brand_model_str = f"{brand} / {model}"
                else:
                    brand_model_str = brand or model or vendor or "Generic"

                result.append({
                    "mac": mac,
                    "mac_clean": mac.replace(":", "").replace("-", ""),
                    "ip": ip,
                    "hostname": hostname,
                    "custom_name": custom_name,
                    "display_name": display_name,
                    "vendor": vendor,
                    "brand": brand,
                    "model": model,
                    "brand_model_str": brand_model_str,
                    "category": cls["category"],
                    "category_label": cls["category_label"],
                    "icon": cls["icon"],
                    "switch_ip": switch_ip,
                    "port": port,
                    "vlan": vlan,
                    "ssid": ssid,
                    "signal_dbm": raw_sig,
                    "signal_percent": signal_percent,
                    "is_wireless": is_wireless,
                    "status": status_str,
                    "is_online": is_online,
                    "speed_tx_bps": speed_tx,
                    "speed_rx_bps": speed_rx,
                    "speed_tx_formatted": format_bps(speed_tx),
                    "speed_rx_formatted": format_bps(speed_rx),
                    "total_tx_bytes": cum_tx,
                    "total_rx_bytes": cum_rx,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "last_seen_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_seen)) if last_seen else "Never",
                    "subnet": cidr,
                })

        # Sort IP numerically
        def ip_sort_key(item):
            try:
                return (0, [int(part) for part in item["ip"].split(".")])
            except Exception:
                return (1, item["mac"])

        result.sort(key=ip_sort_key)
        logger.debug(
            f"Computed client list: returned {len(result)} items (filter subnet={subnet_filter}, "
            f"category={category_filter}, status={status_filter}, search='{search or ''}')"
        )
        return result

    def get_client_details(self, mac: str) -> Optional[Dict[str, Any]]:
        """Returns deep-dive Fing dossier for a client: Network Setup card, WiFi
        Access Points card, Bandwidth charts & gauges, Roaming timeline, and IP history.
        """
        mac_clean = clean_mac_str(mac)
        all_clients = self.device_repo.get_all_discovered_clients()
        client_data = None
        for c in all_clients.values():
            if clean_mac_str(c.get("mac", "")) == mac_clean:
                client_data = c
                break

        if not client_data:
            client_data = {
                "mac": mac_clean,
                "ip": "",
                "hostname": "",
                "vendor": self.vendor_service.lookup_vendor(mac_clean),
                "device_type": "client",
                "switch_ip": "",
                "port": "",
                "vlan": "1",
                "status": "offline",
                "first_seen": 0,
                "last_seen": 0,
            }

        now = time.time()
        ip = client_data.get("ip") or ""
        hostname = client_data.get("hostname") or ""
        custom_name = client_data.get("custom_name") or ""
        vendor = client_data.get("vendor") or self.vendor_service.lookup_vendor(mac_clean)
        switch_ip = client_data.get("switch_ip") or ""
        port = client_data.get("port") or ""
        ssid = client_data.get("ssid") or ""
        raw_sig = client_data.get("signal_dbm")
        last_seen = float(client_data.get("last_seen") or 0)
        is_online = (client_data.get("status") == "online") and (now - last_seen < 600)

        # Classification
        cls = self.classify_device(
            vendor=vendor,
            hostname=hostname,
            custom_name=custom_name,
            device_type=client_data.get("device_type", "client"),
            mac=mac_clean,
        )

        is_wireless = bool(ssid or "wlan" in port.lower() or "ath" in port.lower() or "ra" in port.lower())

        # Network Setup determination
        netmask = "255.255.255.0"
        gateway_ip = ""
        gateway_mac = ""
        dns_server = "1.1.1.1"

        if ip and "." in ip:
            parts = ip.split(".")
            netmask = "255.255.255.0"
            suggested_gw = f"{parts[0]}.{parts[1]}.{parts[2]}.1"
            cfg = load_config()
            for sw in cfg.get("switches", []):
                role = str(sw.get("role") or sw.get("device_type") or "").lower()
                sw_ip = sw.get("ip", "")
                if ("router" in role or "gateway" in role) and sw_ip.startswith(f"{parts[0]}.{parts[1]}.{parts[2]}."):
                    suggested_gw = sw_ip
                    gateway_mac = clean_mac_str(sw.get("mac", ""))
                    break
            gateway_ip = suggested_gw

        # Bandwidth speeds & history
        with self._lock:
            live = self._live_speeds.get(mac_clean)
            if live and hasattr(live, "speed_tx_bps"):
                speed_tx = int(live.speed_tx_bps or 0) if is_online else 0
                speed_rx = int(live.speed_rx_bps or 0) if is_online else 0
                cum_tx = int(live.cum_tx or 0)
                cum_rx = int(live.cum_rx or 0)
            elif isinstance(live, dict):
                speed_tx = int(live.get("speed_tx_bps", 0)) if is_online else 0
                speed_rx = int(live.get("speed_rx_bps", 0)) if is_online else 0
                cum_tx = int(live.get("cum_tx", 0))
                cum_rx = int(live.get("cum_rx", 0))
            else:
                speed_tx = 0
                speed_rx = 0
                cum_tx = 0
                cum_rx = 0

        # Database queries for trends and histories
        trends = self.device_repo.get_client_bandwidth_trends(mac_clean, hours=168)
        connection_history = self.device_repo.get_client_connection_history(mac_clean, limit=50)
        ip_history = self.device_repo.get_client_ip_history(mac_clean)

        # 24h totals from trends
        day_ago = int(now - 86400)
        day_tx = sum(t["tx_bytes"] for t in trends if t["hour_timestamp"] >= day_ago)
        day_rx = sum(t["rx_bytes"] for t in trends if t["hour_timestamp"] >= day_ago)

        dossier = {
            "mac": mac_clean,
            "ip": ip,
            "hostname": hostname,
            "custom_name": custom_name,
            "display_name": custom_name or hostname or cls["model"] or (f"Client {ip}" if ip else f"Client {mac_clean[-8:]}"),
            "vendor": vendor,
            "brand": cls["brand"],
            "model": cls["model"],
            "category": cls["category"],
            "category_label": cls["category_label"],
            "icon": cls["icon"],
            "status": "online" if is_online else "offline",
            "is_online": is_online,
            "is_wireless": is_wireless,
            "network_setup": {
                "netmask": netmask,
                "gateway_ip": gateway_ip,
                "gateway_mac": gateway_mac or "Unknown",
                "local_address": ip or "Unassigned",
                "dns": dns_server,
                "interface_type": "Wi-Fi" if is_wireless else "Ethernet",
            },
            "wifi_setup": {
                "ap_name": switch_ip or "Access Point",
                "bssid": switch_ip or mac_clean,
                "ssid": ssid or "Default Wi-Fi",
                "signal_dbm": raw_sig,
                "signal_percent": signal_dbm_to_percent(raw_sig),
                "security_protocol": "WPA2/WPA3 Personal" if is_wireless else "N/A",
            },
            "bandwidth": {
                "speed_tx_bps": speed_tx,
                "speed_rx_bps": speed_rx,
                "speed_tx_formatted": format_bps(speed_tx),
                "speed_rx_formatted": format_bps(speed_rx),
                "cum_tx_bytes": cum_tx,
                "cum_rx_bytes": cum_rx,
                "cum_tx_formatted": format_bytes(cum_tx),
                "cum_rx_formatted": format_bytes(cum_rx),
                "day_tx_bytes": day_tx,
                "day_rx_bytes": day_rx,
                "day_tx_formatted": format_bytes(day_tx),
                "day_rx_formatted": format_bytes(day_rx),
                "trends": trends,
            },
            "connection_history": connection_history,
            "ip_history": ip_history,
            "first_seen": float(client_data.get("first_seen") or 0),
            "last_seen": last_seen,
        }
        logger.debug(
            f"Fetched client dossier for {mac_clean}: status={dossier['status']}, "
            f"{len(trends)} trend points, {len(connection_history)} connection history entries"
        )
        return dossier


_client_monitor_service: Optional[ClientMonitorService] = None


def get_client_monitor_service() -> ClientMonitorService:
    global _client_monitor_service
    if _client_monitor_service is None:
        _client_monitor_service = ClientMonitorService()
    return _client_monitor_service
