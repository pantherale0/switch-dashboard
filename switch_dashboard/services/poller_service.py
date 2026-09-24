import re
import time
import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional, Tuple

from switch_dashboard.config import get_config, load_config
from switch_dashboard.protocols.base import Capability
from switch_dashboard.protocols.registry import ProtocolRegistry
from switch_dashboard.storage.repositories.metric_repo import MetricRepository
from switch_dashboard.storage.repositories.device_repo import DeviceRepository

logger = logging.getLogger("switch_dashboard.services.poller")


def unify_speed(s: str) -> str:
    if not s:
        return ""
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([mMgGtT]?)[bB]?[pP]?[sS]?$", str(s).strip())
    if m:
        val_str, unit = m.groups()
        val = float(val_str)
        unit = unit.upper()
        if unit == "M" or unit == "":
            if val >= 1000:
                val_g = val / 1000.0
                if val_g.is_integer():
                    return f"{int(val_g)}G"
                else:
                    return f"{val_g:.1f}".rstrip("0").rstrip(".") + "G"
            else:
                if val.is_integer():
                    return f"{int(val)}M"
                else:
                    return f"{val:.1f}".rstrip("0").rstrip(".") + "M"
        elif unit == "G":
            if val.is_integer():
                return f"{int(val)}G"
            else:
                return f"{val:.1f}".rstrip("0").rstrip(".") + "G"
    return str(s)


def format_bps(bps: int) -> str:
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.1f} Gbps"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.1f} Kbps"
    return f"{bps} bps"


def calculate_counter_delta(cur_bytes: int, prev_bytes: int, max_bytes_limit: float) -> int:
    """Calculates non-negative byte delta between polling intervals handling 32-bit rollover and counter resets."""
    if cur_bytes >= prev_bytes:
        delta = cur_bytes - prev_bytes
    else:
        # Counter decreased: check if 32-bit rollover occurred (around 2^32 = 4,294,967,296)
        if 2_000_000_000 < prev_bytes <= 4_294_967_296:
            wrapped = (4_294_967_296 - prev_bytes) + cur_bytes
            delta = wrapped if (0 <= wrapped <= max_bytes_limit) else 0
        elif 0 <= cur_bytes <= max_bytes_limit:
            # Device rebooted or counter reset to zero
            delta = cur_bytes
        else:
            delta = 0

    if delta < 0 or delta > max_bytes_limit:
        return 0
    return int(delta)


class PollerService:
    def __init__(self, metric_repo: Optional[MetricRepository] = None, device_repo: Optional[DeviceRepository] = None):
        self.metric_repo = metric_repo or MetricRepository()
        self.device_repo = device_repo or DeviceRepository()
        self._cached_data: Dict[str, Dict[str, Any]] = {}
        self._cached_speeds: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._mac_tables: Dict[str, List[Dict[str, Any]]] = {}
        self._last_mac_scrape_times: Dict[str, float] = {}
        self._max_poll_workers = 4

        # Warm up cached device state, speeds, and MAC tables from database repository on startup
        try:
            cached_data, cached_speeds = self.device_repo.load_all_cached_device_states()
            if cached_data:
                self._cached_data = cached_data
                self._cached_speeds = cached_speeds
                logger.info(f"Loaded {len(cached_data)} cached device snapshots from database on startup.")
        except Exception as e:
            logger.warning(f"Could not warm up cached device state from database: {e}")

        try:
            cfg = get_config()
            self._max_poll_workers = cfg.get("settings", {}).get("max_poll_workers", 4)
            for sw in cfg.get("switches", []):
                s_ip = sw.get("ip")
                if s_ip:
                    db_tbl = self.device_repo.get_mac_table(s_ip)
                    if db_tbl:
                        self._mac_tables[s_ip] = db_tbl
        except Exception as e:
            logger.warning(f"Could not warm up MAC tables from database: {e}")

    def get_cached_data(self) -> Dict[str, Dict[str, Any]]:
        with self._cache_lock:
            return dict(self._cached_data)

    def get_cached_speeds(self) -> Dict[str, Dict[str, Any]]:
        with self._cache_lock:
            return dict(self._cached_speeds)

    def get_switch_data(self, ip: str) -> Optional[Dict[str, Any]]:
        with self._cache_lock:
            return self._cached_data.get(ip)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="PollerThread")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def scrape_mac_table_for_switch(self, ip: str) -> List[Dict[str, Any]]:
        cfg = get_config()
        sw = next((s for s in cfg.get("switches", []) if s.get("ip") == ip), None)
        if not sw:
            return []
        protocol = ProtocolRegistry.create(sw)
        try:
            table = protocol.scrape_mac_table()
            with self._cache_lock:
                self._mac_tables[ip] = table
                self._last_mac_scrape_times[ip] = time.time()
                if ip in self._cached_data:
                    self._cached_data[ip]["mac_table"] = table
                    self._cached_data[ip]["mac_timestamp"] = self._last_mac_scrape_times[ip]
            self.device_repo.update_mac_table(ip, table, time.time())
            return table
        except Exception as e:
            logger.error(f"Error scraping MAC table for {ip}: {e}")
            return self._mac_tables.get(ip) or self.device_repo.get_mac_table(ip)

    def _run_loop(self):
        logger.info("Switch Poller background loop started.")
        while not self._stop_event.is_set():
            try:
                self.poll_all_switches()
            except Exception as e:
                logger.error(f"Unexpected error in switch poller loop: {e}", exc_info=True)

            cfg = get_config()
            interval = cfg.get("settings", {}).get("refresh_interval", 30)
            if self._stop_event.wait(max(5, interval)):
                break

    def _poll_single_switch(
        self,
        sw: dict,
        counters: dict,
        now: float,
        settings: dict,
    ) -> Tuple[str, dict, dict, list, dict]:
        """Poll a single switch and return its results.

        Returns:
            (ip, data, sw_speeds, metric_samples, counter_updates)
        """
        ip = sw["ip"]
        model_lower = str(sw.get("model", "")).lower()
        refresh_interval = settings.get("refresh_interval", 30)
        mac_refresh_multiplier = settings.get("mac_refresh_multiplier", 5)

        # Handle "internet" pseudo-device
        if model_lower == "internet":
            data = {
                "name": sw.get("name", "Internet"),
                "ip": ip,
                "ports": [],
                "status": "online",
                "mac_table": [],
                "mac_timestamp": now,
                "mac": "",
                "model": "internet",
                "timestamp": now,
            }
            return (ip, data, {}, [], {})

        # Create protocol driver
        try:
            protocol = ProtocolRegistry.create(sw)
        except Exception as e:
            logger.error(f"Failed to create protocol driver for switch {ip}: {e}")
            data = {"name": sw.get("name", ip), "ip": ip, "ports": [], "error": str(e)}
            return (ip, data, {}, [], {})

        # Scrape telemetry
        logger.debug(f"Polling switch {sw.get('name', ip)} ({ip}) via {sw.get('protocol', 'default')}...")
        try:
            data = protocol.scrape()
            data.setdefault("role", sw.get("role", sw.get("device_type", "switch")))
            data.setdefault("device_type", sw.get("device_type", data.get("role", "switch")))
            data.setdefault("protocol", protocol.protocol_name)
            if protocol.has_capability(Capability.CHILD_DEVICES) and "child_devices" not in data:
                data["child_devices"] = protocol.scrape_child_devices()
            data.setdefault("child_devices", [])
            if data and "ports" in data:
                for p in data["ports"]:
                    if "speed" in p:
                        p["speed"] = unify_speed(p["speed"])
        except Exception as e:
            logger.error(f"Error scraping telemetry for switch {ip}: {e}")
            data = {"name": sw.get("name", ip), "ip": ip, "ports": [], "error": str(e), "status": "offline"}

        status = data.get("status")
        if status is None:
            status = "offline" if ("error" in data or not data.get("ports")) else "online"
            data["status"] = status
        is_online = (status == "online")
        if data.get("mac") and not sw.get("mac"):
            sw["mac"] = data["mac"]

        # Check if MAC table scrape is due (only when switch is online)
        if is_online:
            last_scrape = self._last_mac_scrape_times.get(ip, 0)
            mac_scrape_due = (
                now - last_scrape >= (refresh_interval * mac_refresh_multiplier)
                or ip not in self._mac_tables
            )
            if protocol.has_capability(Capability.MAC_TABLE) and mac_scrape_due:
                try:
                    mac_table = protocol.scrape_mac_table()
                    self._mac_tables[ip] = mac_table
                    self._last_mac_scrape_times[ip] = now
                    self.device_repo.update_mac_table(ip, mac_table, now)
                except Exception as e:
                    logger.error(f"Error scraping MAC table for {ip}: {e}")
                    if ip not in self._mac_tables or not self._mac_tables[ip]:
                        self._mac_tables[ip] = self.device_repo.get_mac_table(ip)

            elif not protocol.has_capability(Capability.MAC_TABLE):
                self._mac_tables[ip] = data.get("mac_table", [])

            if protocol.has_capability(Capability.NEIGHBORS):
                try:
                    data["neighbors"] = protocol.scrape_neighbors()
                except Exception as e:
                    logger.error(f"Error scraping neighbors for {ip}: {e}")
                    data["neighbors"] = []
            else:
                data["neighbors"] = []
        else:
            data["neighbors"] = []

        data["mac_table"] = self._mac_tables.get(ip, [])
        data["mac_timestamp"] = self._last_mac_scrape_times.get(ip, 0)

        # Cumulative counters calculation & rate math
        counter_updates: Dict[str, Any] = {}
        metric_samples: List[Dict[str, Any]] = []

        for p in data.get("ports", []):
            port = str(p.get("port", ""))
            key = f"{ip}:{port}"
            last = counters.get(key, {"tx": 0, "rx": 0, "cum_tx": 0, "cum_rx": 0, "ts": None})
            cur_tx = max(0, int(p.get("tx_bytes") or 0))
            cur_rx = max(0, int(p.get("rx_bytes") or 0))

            last_ts = last.get("ts")
            if last_ts is None:
                delta_tx = 0
                delta_rx = 0
            else:
                speed_str = p.get("speed", "")
                link_speed_bps = 10_000_000_000
                if speed_str:
                    s_lower = str(speed_str).lower()
                    if "10g" in s_lower:
                        link_speed_bps = 10_000_000_000
                    elif "2.5g" in s_lower or "2500" in s_lower:
                        link_speed_bps = 2_500_000_000
                    elif "2g" in s_lower or "2000" in s_lower:
                        link_speed_bps = 2_000_000_000
                    elif "1g" in s_lower or "1000" in s_lower:
                        link_speed_bps = 1_000_000_000
                    elif "100m" in s_lower or "100" in s_lower:
                        link_speed_bps = 100_000_000
                    elif "10m" in s_lower or "10" in s_lower:
                        link_speed_bps = 10_000_000

                dt = now - last_ts
                if dt <= 0:
                    dt = 15.0

                max_bytes_limit = (link_speed_bps * dt * 1.5) / 8

                prev_tx = max(0, int(last.get("tx") or 0))
                prev_rx = max(0, int(last.get("rx") or 0))

                delta_tx = calculate_counter_delta(cur_tx, prev_tx, max_bytes_limit)
                delta_rx = calculate_counter_delta(cur_rx, prev_rx, max_bytes_limit)

            # Cumulative total traffic calculation (guaranteed non-negative with auto self-healing)
            last_cum_tx = last.get("cum_tx")
            last_cum_rx = last.get("cum_rx")

            if last_cum_tx is None or last_cum_tx < 0:
                last_cum_tx = cur_tx
            if last_cum_rx is None or last_cum_rx < 0:
                last_cum_rx = cur_rx

            cum_tx = max(0, last_cum_tx + delta_tx)
            cum_rx = max(0, last_cum_rx + delta_rx)

            counter_updates[key] = {
                "tx": cur_tx,
                "rx": cur_rx,
                "cum_tx": cum_tx,
                "cum_rx": cum_rx,
                "ts": now,
            }
            p["cum_tx"] = cum_tx
            p["cum_rx"] = cum_rx

            # Update live ring buffer
            self.metric_repo.append_live_sample(ip, port, now, cum_tx, cum_rx)

            # Compute current speed bps
            live_h = self.metric_repo.get_live_history(ip, port)
            speed_tx_bps = 0
            speed_rx_bps = 0
            if len(live_h) >= 2:
                p1 = live_h[-2]
                p2 = live_h[-1]
                dt = p2["ts"] - p1["ts"]
                if dt > 0:
                    speed_tx = (p2["tx"] - p1["tx"]) * 8 / dt
                    speed_rx = (p2["rx"] - p1["rx"]) * 8 / dt
                    speed_tx_bps = max(0, int(speed_tx))
                    speed_rx_bps = max(0, int(speed_rx))

            p["speed_tx_bps"] = speed_tx_bps
            p["speed_rx_bps"] = speed_rx_bps

            # Append to hourly in-memory series
            max_hourly_pts = max(1, int(3600 / max(5, refresh_interval)))
            self.metric_repo.append_hourly_sample(ip, port, now, speed_tx_bps, speed_rx_bps, max_hourly_pts)

            # Append to daily in-memory series (15-minute averages)
            daily_pts = self.metric_repo.get_daily_history(ip, port)
            if len(daily_pts) == 0 or now - daily_pts[-1]["ts"] >= 900:
                avg_tx, avg_rx = self.metric_repo.get_avg_speed(ip, port, 900)
                self.metric_repo.append_daily_sample(ip, port, now, avg_tx, avg_rx, 96)

            # Prepare sample for SQLite metric_history batch insert
            metric_samples.append({
                "device_ip": ip,
                "port": port,
                "ts": now,
                "tx_bytes": cur_tx,
                "rx_bytes": cur_rx,
                "speed_tx_bps": speed_tx_bps,
                "speed_rx_bps": speed_rx_bps,
            })

        # Cumulative counters and rate math for virtual child devices (e.g. Proxmox VMs / LXCs)
        for child in data.get("child_devices", []):
            cid = str(child.get("native_id") or "")
            if not cid:
                continue
            key = f"{ip}:guest:{cid}"
            port_key = f"guest:{cid}"
            last = counters.get(key) or counters.get(f"{ip}:{cid}", {"tx": 0, "rx": 0, "cum_tx": 0, "cum_rx": 0, "ts": None})
            cur_tx = max(0, int(child.get("tx_bytes") or child.get("netout") or 0))
            cur_rx = max(0, int(child.get("rx_bytes") or child.get("netin") or 0))

            last_ts = last.get("ts")
            if last_ts is None:
                delta_tx = 0
                delta_rx = 0
            else:
                link_speed_bps = 10_000_000_000
                dt = now - last_ts
                if dt <= 0:
                    dt = 15.0
                max_bytes_limit = (link_speed_bps * dt * 1.5) / 8

                prev_tx = max(0, int(last.get("tx") or 0))
                prev_rx = max(0, int(last.get("rx") or 0))

                delta_tx = calculate_counter_delta(cur_tx, prev_tx, max_bytes_limit)
                delta_rx = calculate_counter_delta(cur_rx, prev_rx, max_bytes_limit)

            last_cum_tx = last.get("cum_tx")
            last_cum_rx = last.get("cum_rx")

            if last_cum_tx is None or last_cum_tx < 0:
                last_cum_tx = cur_tx
            if last_cum_rx is None or last_cum_rx < 0:
                last_cum_rx = cur_rx

            cum_tx = max(0, last_cum_tx + delta_tx)
            cum_rx = max(0, last_cum_rx + delta_rx)

            counter_updates[key] = {
                "tx": cur_tx,
                "rx": cur_rx,
                "cum_tx": cum_tx,
                "cum_rx": cum_rx,
                "ts": now,
            }
            child["cum_tx"] = cum_tx
            child["cum_rx"] = cum_rx
            child["tx_bytes"] = cur_tx
            child["rx_bytes"] = cur_rx

            # Update live ring buffer (support both guest:cid and cid keys)
            self.metric_repo.append_live_sample(ip, port_key, now, cum_tx, cum_rx)
            self.metric_repo.append_live_sample(ip, cid, now, cum_tx, cum_rx)

            # Compute current speed bps
            live_h = self.metric_repo.get_live_history(ip, port_key)
            speed_tx_bps = 0
            speed_rx_bps = 0
            if len(live_h) >= 2:
                p1 = live_h[-2]
                p2 = live_h[-1]
                dt = p2["ts"] - p1["ts"]
                if dt > 0:
                    speed_tx = (p2["tx"] - p1["tx"]) * 8 / dt
                    speed_rx = (p2["rx"] - p1["rx"]) * 8 / dt
                    speed_tx_bps = max(0, int(speed_tx))
                    speed_rx_bps = max(0, int(speed_rx))

            child["speed_tx_bps"] = speed_tx_bps
            child["speed_rx_bps"] = speed_rx_bps

            # Append to hourly in-memory series
            max_hourly_pts = max(1, int(3600 / max(5, refresh_interval)))
            self.metric_repo.append_hourly_sample(ip, port_key, now, speed_tx_bps, speed_rx_bps, max_hourly_pts)
            self.metric_repo.append_hourly_sample(ip, cid, now, speed_tx_bps, speed_rx_bps, max_hourly_pts)

            # Append to daily in-memory series (15-minute averages)
            daily_pts = self.metric_repo.get_daily_history(ip, port_key)
            if len(daily_pts) == 0 or now - daily_pts[-1]["ts"] >= 900:
                avg_tx, avg_rx = self.metric_repo.get_avg_speed(ip, port_key, 900)
                self.metric_repo.append_daily_sample(ip, port_key, now, avg_tx, avg_rx, 96)
                self.metric_repo.append_daily_sample(ip, cid, now, avg_tx, avg_rx, 96)

            # Prepare sample for SQLite metric_history batch insert
            metric_samples.append({
                "device_ip": ip,
                "port": port_key,
                "ts": now,
                "tx_bytes": cur_tx,
                "rx_bytes": cur_rx,
                "speed_tx_bps": speed_tx_bps,
                "speed_rx_bps": speed_rx_bps,
            })

        sw_speeds: Dict[str, Any] = {}
        for p in data.get("ports", []):
            sw_speeds[str(p.get("port", ""))] = {
                "speed_tx": p.get("speed_tx_bps", 0),
                "speed_rx": p.get("speed_rx_bps", 0),
            }
        for child in data.get("child_devices", []):
            cid = str(child.get("native_id") or "")
            if cid:
                sw_speeds[cid] = {
                    "speed_tx": child.get("speed_tx_bps", 0),
                    "speed_rx": child.get("speed_rx_bps", 0),
                }
                sw_speeds[f"guest:{cid}"] = {
                    "speed_tx": child.get("speed_tx_bps", 0),
                    "speed_rx": child.get("speed_rx_bps", 0),
                }

        return (ip, data, sw_speeds, metric_samples, counter_updates)

    def poll_all_switches(self):
        cfg = load_config()
        switch_configs = cfg.get("switches", [])
        settings = cfg.get("settings", {})

        counters = self.metric_repo.load_counters()
        now = time.time()
        results: Dict[str, Dict[str, Any]] = {}
        speeds: Dict[str, Dict[str, Any]] = {}
        all_metric_samples: List[Dict[str, Any]] = []

        # Prune disabled switches from cache
        enabled_ips = {sw["ip"] for sw in switch_configs if sw.get("enabled", True)}
        with self._cache_lock:
            for ip in list(self._cached_data.keys()):
                if ip not in enabled_ips:
                    del self._cached_data[ip]

        # Filter to enabled switches only
        active_switches = [sw for sw in switch_configs if sw.get("enabled", True)]

        if not active_switches:
            with self._cache_lock:
                self._cached_data = results
                self._cached_speeds = speeds
            return

        max_workers = min(len(active_switches), self._max_poll_workers)

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="Poller") as executor:
            futures = {
                executor.submit(self._poll_single_switch, sw, counters, now, settings): sw
                for sw in active_switches
            }

            for future in as_completed(futures):
                # Check for graceful shutdown
                if self._stop_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                sw = futures[future]
                ip = sw["ip"]
                try:
                    r_ip, data, sw_speeds, samples, counter_updates = future.result()
                    results[r_ip] = data
                    speeds[r_ip] = sw_speeds
                    all_metric_samples.extend(samples)
                    counters.update(counter_updates)
                except Exception as e:
                    logger.error(f"Poll worker failed for {ip}: {e}", exc_info=True)
                    results[ip] = {"name": sw.get("name", ip), "ip": ip, "ports": [], "error": str(e)}
                    speeds[ip] = {}

        # Save counters and raw metric samples into SQLite
        try:
            self.metric_repo.save_counters(counters)
            self.metric_repo.record_samples_batch(all_metric_samples)
        except Exception as e:
            logger.error(f"Error persisting metrics to database: {e}")

        # Persist device snapshots and speeds into SQLite for instant warm boot
        try:
            self.device_repo.save_all_cached_device_states(results, speeds)
        except Exception as e:
            logger.error(f"Error persisting device cache to database: {e}")

        # Active Client Monitoring processing (mobility, roaming, IP history, client bandwidth)
        try:
            from switch_dashboard.services.clients import get_client_monitor_service
            get_client_monitor_service().process_polled_cycle(results, now)
        except Exception as e:
            logger.error(f"Error in client monitor service processing: {e}", exc_info=True)

        with self._cache_lock:
            self._cached_data = results
            self._cached_speeds = speeds


_poller_service: Optional[PollerService] = None


def get_poller_service() -> PollerService:
    global _poller_service
    if _poller_service is None:
        _poller_service = PollerService()
    return _poller_service
