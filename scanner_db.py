"""Scanner Database Facade (Backwards Compatibility)."""

import os
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

from switch_dashboard.config import DATABASE_PATH, DATA_DIR
from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.repositories.scanner_repo import ScannerRepository

logger = logging.getLogger("scanner_db")

DB_DIR = DATA_DIR
DB_PATH = DATABASE_PATH


def get_db_connection():
    db = get_db()
    return db.get_connection()


def init_db():
    db = get_db()
    db.init_db()


def load_state_from_db():
    repo = ScannerRepository()
    return repo.load_state_from_db()


def update_db_and_get_status(
    current_scan_results,
    last_db_state,
    ports_to_scan,
    perform_port_scan,
    port_scan_enabled,
    port_scan_timeout,
    port_scan_threads,
):
    from switch_dashboard.services.scanner_service import scan_ports_threaded
    from switch_dashboard.services.vendor_service import get_vendor_service

    vendor_service = get_vendor_service()
    repo = ScannerRepository()
    return repo.update_db_and_get_status(
        current_scan_results=current_scan_results,
        last_db_state=last_db_state,
        ports_to_scan=ports_to_scan,
        perform_port_scan=perform_port_scan,
        port_scan_enabled=port_scan_enabled,
        port_scan_timeout=port_scan_timeout,
        port_scan_threads=port_scan_threads,
        vendor_lookup_fn=vendor_service.lookup_vendor,
        port_scan_fn=scan_ports_threaded,
    )


def update_host_field(ip_address, field_name, new_value):
    repo = ScannerRepository()
    if field_name == "hostname":
        repo.update_host(ip_address, hostname=new_value)
        return True
    elif field_name == "note":
        repo.update_host(ip_address, note=new_value)
        return True
    return False


def update_known_host(ip_address, new_known_state):
    repo = ScannerRepository()
    repo.toggle_known_host(ip_address, bool(new_known_state))
    return True


def delete_host(ip_address):
    repo = ScannerRepository()
    repo.delete_host(ip_address)
    return True


def get_history_data():
    from collections import defaultdict
    grouped = defaultdict(lambda: {"hostname": "", "events": []})
    db = get_db()
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ip_address, hostname FROM hosts")
        for row in cur.fetchall():
            grouped[row["ip_address"]]["hostname"] = row["hostname"] or ""

        cur.execute("SELECT ip_address, status, event_time FROM host_history ORDER BY ip_address, event_time ASC")
        for row in cur.fetchall():
            ip = row["ip_address"]
            ts = row["event_time"]
            if ts:
                try:
                    if "T" not in ts:
                        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                        ts_str = dt.isoformat() + "Z"
                    else:
                        ts_str = ts
                except Exception:
                    ts_str = ts
            else:
                ts_str = ""

            grouped[ip]["events"].append({
                "status": int(row["status"]),
                "event_time": ts_str,
            })
    return dict(grouped)


def delete_host_history(ip_address):
    repo = ScannerRepository()
    repo.delete_host_history(ip_address)
    return 1


def delete_all_history():
    repo = ScannerRepository()
    repo.clear_all_history()
    return 1


def purge_old_history(hours_to_keep):
    if hours_to_keep <= 0:
        return
    db = get_db()
    cutoff_date = (datetime.now(timezone.utc) - timedelta(hours=hours_to_keep)).strftime("%Y-%m-%d %H:%M:%S")
    with db.transaction() as cur:
        purge_query = """
            DELETE FROM host_history
            WHERE event_time < ?
              AND id NOT IN (
                  SELECT MAX(id)
                  FROM host_history
                  GROUP BY ip_address
              )
        """
        cur.execute(purge_query, (cutoff_date,))
        logger.info(f"History purge complete. Deleted {cur.rowcount} old records.")


def insert_host_history_batch(history_inserts):
    if not history_inserts:
        return
    db = get_db()
    with db.transaction() as cur:
        cur.executemany("INSERT INTO host_history (ip_address, status, event_time) VALUES (?, ?, ?)", history_inserts)
