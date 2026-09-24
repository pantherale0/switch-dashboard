import os
import time
import socket
import logging
import threading
import concurrent.futures
from ipaddress import ip_network, ip_address
from typing import Dict, Set, Optional, Any

from switch_dashboard.config import DATA_DIR, get_config, get_setting
from switch_dashboard.storage.repositories.scanner_repo import ScannerRepository
from switch_dashboard.services.vendor_service import get_vendor_service

logger = logging.getLogger("switch_dashboard.services.scanner")

try:
    from scapy.all import Ether, ARP, IP, ICMP, srp, sr1, conf
    conf.verb = 0
    scapy_available = True
except Exception as e:
    logger.debug(f"Scapy not fully loaded: {e}. Active scanner will only operate on container.")
    scapy_available = False


def parse_port_range(range_str: str) -> Set[int]:
    """Parses port ranges like '22,80,443,8000-8080' into a set of port integers."""
    ports = set()
    if not range_str:
        return ports
    try:
        for part in str(range_str).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = map(int, part.split("-", 1))
                if 1 <= start <= end <= 65535:
                    ports.update(range(start, end + 1))
                else:
                    logger.warning(f"Invalid port range ignored: {part}")
            else:
                port_num = int(part)
                if 1 <= port_num <= 65535:
                    ports.add(port_num)
                else:
                    logger.warning(f"Invalid port number ignored: {part}")
    except ValueError:
        logger.error(f"Invalid port range format: '{range_str}'")
        return set()
    return ports


def scan_port(ip: str, port: int, timeout: float) -> bool:
    """Attempts to connect to a TCP port on a given IP. Returns True if open."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        return sock.connect_ex((ip, port)) == 0
    except socket.error:
        return False
    finally:
        if sock:
            sock.close()


def scan_ports_threaded(ip: str, ports_to_scan: Set[int], timeout: float, max_threads: int) -> str:
    """Scans multiple ports concurrently using a ThreadPoolExecutor."""
    open_ports = []
    if not ports_to_scan:
        return ""

    actual_threads = min(max_threads, len(ports_to_scan))
    if actual_threads <= 0:
        actual_threads = 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=actual_threads) as executor:
        future_to_port = {executor.submit(scan_port, ip, port, timeout): port for port in ports_to_scan}
        for future in concurrent.futures.as_completed(future_to_port):
            port = future_to_port[future]
            try:
                if future.result():
                    open_ports.append(port)
            except Exception as exc:
                logger.warning(f"Exception scanning {ip}:{port} - {exc}")

    if open_ports:
        open_ports.sort()
        return ",".join(map(str, open_ports))
    return ""


def scan_network(network_cidr: str) -> Optional[Dict[str, Dict[str, str]]]:
    """Runs a Scapy ARP broadcast scan over the target subnet CIDR."""
    active_hosts = {}
    if not scapy_available:
        logger.warning("Scapy is not available. Skipping network scan.")
        return None

    logger.info(f"Starting ARP discovery scan on {network_cidr}...")
    try:
        network_obj = ip_network(network_cidr, strict=False)
        packet = Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=str(network_cidr))
        answered, _ = srp(packet, timeout=2, retry=1, verbose=False)
        logger.info(f"ARP discovery completed. {len(answered)} hosts responded.")

        for _, received in answered:
            ip = received.psrc
            mac = received.hwsrc
            try:
                if ip_address(ip) in network_obj:
                    active_hosts[ip] = {"mac": mac}
            except ValueError:
                logger.debug(f"Ignoring invalid IP received: '{ip}'")
    except PermissionError:
        logger.critical("Root privileges required to run Scapy ARP scan.")
        return None
    except Exception as e:
        logger.error(f"Error during network ARP scan: {e}")
        return None

    return active_hosts


def is_host_reachable_by_ping(ip_addr: str, timeout: float = 1.0, retry: int = 1) -> bool:
    """Sends an ICMP Echo Request ping check using Scapy."""
    if not scapy_available or not ip_addr:
        return False
    try:
        response = sr1(IP(dst=ip_addr) / ICMP(), timeout=timeout, retry=retry, verbose=False)
        return response is not None
    except Exception as e:
        logger.warning(f"Ping check error to {ip_addr}: {e}")
        return False


class ScannerService:
    def __init__(self, scanner_repo: Optional[ScannerRepository] = None):
        self.scanner_repo = scanner_repo or ScannerRepository()
        self.vendor_service = get_vendor_service()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ScannerThread")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run_loop(self):
        logger.info("Scanner daemon loop started.")
        while not self._stop_event.is_set():
            cfg = get_config()
            scanner_enabled = cfg.get("scanner_enabled", False)

            if scanner_enabled:
                network_range = cfg.get("scanner_network_range", "192.168.1.0/24")
                port_scan_enabled = cfg.get("scanner_port_scan_enabled", True)
                port_scan_range_str = cfg.get("scanner_port_scan_range", "22,80,443,8080")
                port_scan_interval = cfg.get("scanner_port_scan_interval", 300)

                do_port_scan = False
                now_ts = time.time()
                state_file = os.path.join(DATA_DIR, "last_port_scan.ts")
                last_scan_ts = 0.0
                if os.path.exists(state_file):
                    try:
                        with open(state_file, "r") as f:
                            last_scan_ts = float(f.read().strip())
                    except Exception:
                        pass

                if now_ts - last_scan_ts >= port_scan_interval:
                    do_port_scan = True
                    try:
                        with open(state_file, "w") as f:
                            f.write(str(now_ts))
                    except Exception as e:
                        logger.error(f"Failed to update port scan state file: {e}")

                ports_to_scan = parse_port_range(port_scan_range_str)
                current_scan = scan_network(network_range)

                if current_scan is not None:
                    port_scan_threads = cfg.get("scanner_port_scan_threads", 20)
                    port_scan_timeout_ms = cfg.get("scanner_port_scan_timeout_ms", 500)
                    port_scan_timeout = port_scan_timeout_ms / 1000.0

                    last_state = self.scanner_repo.load_state_from_db()
                    self.scanner_repo.update_db_and_get_status(
                        current_scan_results=current_scan,
                        last_db_state=last_state,
                        ports_to_scan=ports_to_scan,
                        perform_port_scan=do_port_scan,
                        port_scan_enabled=port_scan_enabled,
                        port_scan_timeout=port_scan_timeout,
                        port_scan_threads=port_scan_threads,
                        vendor_lookup_fn=self.vendor_service.lookup_vendor,
                        port_scan_fn=scan_ports_threaded,
                    )

            interval = cfg.get("scanner_interval", 60)
            if self._stop_event.wait(interval):
                break


_scanner_service: Optional[ScannerService] = None


def get_scanner_service() -> ScannerService:
    global _scanner_service
    if _scanner_service is None:
        _scanner_service = ScannerService()
    return _scanner_service
