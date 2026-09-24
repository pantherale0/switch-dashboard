import logging
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Generator

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.engine import get_db_session
from switch_dashboard.storage.models.scanner import ScannerHost, ScannerHostHistory

logger = logging.getLogger("switch_dashboard.storage.scanner_repo")


class ScannerRepository:
    def __init__(self, db=None, db_url: Optional[str] = None):
        self.db = db or get_db()
        self.db_url = db_url

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        if self.db and hasattr(self.db, "session"):
            with self.db.session() as session:
                yield session
        else:
            with get_db_session(self.db_url) as session:
                yield session

    def load_state_from_db(self) -> Dict[str, Dict[str, Any]]:
        state = {}
        try:
            with self._get_session() as session:
                rows = session.execute(select(ScannerHost)).scalars().all()
                for row in rows:
                    state[row.ip_address] = {
                        "ip_address": row.ip_address,
                        "mac_address": row.mac_address or "",
                        "vendor": row.vendor or "",
                        "hostname": row.hostname or "",
                        "ports": row.ports or "",
                        "note": row.note or "",
                        "status": row.status or "",
                        "known_host": int(row.known_host or 0),
                        "first_seen": row.first_seen or "",
                        "last_seen_online": row.last_seen_online or "",
                        "last_updated": row.last_updated or "",
                    }
        except Exception as e:
            logger.error(f"Error loading scanner DB state: {e}")
        return state

    def update_db_and_get_status(
        self,
        current_scan_results: Dict[str, Any],
        last_db_state: Dict[str, Any],
        ports_to_scan: Any,
        perform_port_scan: bool,
        port_scan_enabled: bool,
        port_scan_timeout: float,
        port_scan_threads: int,
        vendor_lookup_fn: Optional[Any] = None,
        port_scan_fn: Optional[Any] = None,
    ) -> Dict[str, Any]:
        final_report_state = OrderedDict()
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        history_inserts = []

        online_ips = set(current_scan_results.keys())

        for ip in online_ips:
            data = current_scan_results[ip]
            mac = data.get("mac", "")
            vendor = vendor_lookup_fn(mac) if vendor_lookup_fn else "Unknown"
            ports_result_str = None

            if port_scan_enabled and perform_port_scan and ports_to_scan and port_scan_fn:
                ports_result_str = port_scan_fn(ip, ports_to_scan, port_scan_timeout, port_scan_threads)

            existing = last_db_state.get(ip)
            first_seen = existing.get("first_seen", now_str) if existing else now_str
            known_host = existing.get("known_host", 0) if existing else 0
            note = existing.get("note", "") if existing else ""
            hostname = existing.get("hostname", "") if existing else ""
            old_ports = existing.get("ports", "") if existing else ""

            final_ports = ports_result_str if ports_result_str is not None else old_ports

            host_dict = {
                "ip_address": ip,
                "mac_address": mac,
                "vendor": vendor,
                "hostname": hostname,
                "ports": final_ports,
                "note": note,
                "status": "Online",
                "known_host": known_host,
                "first_seen": first_seen,
                "last_seen_online": now_str,
                "last_updated": now_str,
            }
            final_report_state[ip] = host_dict

            # Check status transition to record event
            if not existing or existing.get("status") != "Online":
                history_inserts.append((ip, 1, now_str))

        # Check offline hosts
        for ip, host in last_db_state.items():
            if ip not in online_ips:
                h = dict(host)
                h["status"] = "Offline"
                h["last_updated"] = now_str
                final_report_state[ip] = h
                if host.get("status") != "Offline":
                    history_inserts.append((ip, 0, now_str))

        with self._get_session() as session:
            for ip, d in final_report_state.items():
                host = session.get(ScannerHost, ip)
                if host:
                    host.mac_address = d["mac_address"]
                    host.vendor = d["vendor"]
                    host.hostname = d["hostname"]
                    host.ports = d["ports"]
                    host.note = d["note"]
                    host.status = d["status"]
                    host.known_host = d["known_host"]
                    host.first_seen = d["first_seen"]
                    host.last_seen_online = d["last_seen_online"]
                    host.last_updated = d["last_updated"]
                else:
                    session.add(ScannerHost(
                        ip_address=d["ip_address"],
                        mac_address=d["mac_address"],
                        vendor=d["vendor"],
                        hostname=d["hostname"],
                        ports=d["ports"],
                        note=d["note"],
                        status=d["status"],
                        known_host=d["known_host"],
                        first_seen=d["first_seen"],
                        last_seen_online=d["last_seen_online"],
                        last_updated=d["last_updated"],
                    ))

            for ip, status, event_time in history_inserts:
                session.add(ScannerHostHistory(
                    ip_address=ip,
                    status=status,
                    event_time=event_time,
                ))

        return final_report_state

    def get_all_hosts_list(self) -> List[Dict[str, Any]]:
        with self._get_session() as session:
            rows = session.execute(select(ScannerHost).order_by(ScannerHost.ip_address.asc())).scalars().all()
            return [
                {
                    "ip_address": r.ip_address,
                    "mac_address": r.mac_address or "",
                    "vendor": r.vendor or "",
                    "hostname": r.hostname or "",
                    "ports": r.ports or "",
                    "note": r.note or "",
                    "status": r.status or "",
                    "known_host": int(r.known_host or 0),
                    "first_seen": r.first_seen or "",
                    "last_seen_online": r.last_seen_online or "",
                    "last_updated": r.last_updated or "",
                }
                for r in rows
            ]

    def update_host(self, ip: str, hostname: Optional[str] = None, note: Optional[str] = None):
        if hostname is None and note is None:
            return
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_session() as session:
            host = session.get(ScannerHost, ip)
            if host:
                if hostname is not None:
                    host.hostname = hostname
                if note is not None:
                    host.note = note
                host.last_updated = now_str

    def delete_host(self, ip: str):
        with self._get_session() as session:
            host = session.get(ScannerHost, ip)
            if host:
                session.delete(host)
            session.execute(delete(ScannerHostHistory).where(ScannerHostHistory.ip_address == ip))

    def toggle_known_host(self, ip: str, is_known: bool):
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_session() as session:
            host = session.get(ScannerHost, ip)
            if host:
                host.known_host = 1 if is_known else 0
                host.last_updated = now_str

    def get_all_history(self) -> List[Dict[str, Any]]:
        with self._get_session() as session:
            rows = session.execute(select(ScannerHostHistory).order_by(ScannerHostHistory.id.desc())).scalars().all()
            return [
                {"id": r.id, "ip_address": r.ip_address, "status": r.status, "event_time": r.event_time}
                for r in rows
            ]

    def get_host_history(self, ip: str) -> List[Dict[str, Any]]:
        with self._get_session() as session:
            rows = session.execute(
                select(ScannerHostHistory)
                .where(ScannerHostHistory.ip_address == ip)
                .order_by(ScannerHostHistory.id.desc())
            ).scalars().all()
            return [
                {"id": r.id, "ip_address": r.ip_address, "status": r.status, "event_time": r.event_time}
                for r in rows
            ]

    def delete_host_history(self, ip: str):
        with self._get_session() as session:
            session.execute(delete(ScannerHostHistory).where(ScannerHostHistory.ip_address == ip))

    def clear_all_history(self):
        with self._get_session() as session:
            session.execute(delete(ScannerHostHistory))
