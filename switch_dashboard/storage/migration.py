import os
import json
import sqlite3
import shutil
import logging
from typing import Dict, Any

from switch_dashboard.config import (
    COUNTERS_PATH,
    HOURLY_PATH,
    DAILY_PATH,
    LEGACY_SCANNER_DB_PATH,
    DATABASE_PATH,
)
from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.repositories.metric_repo import MetricRepository

logger = logging.getLogger("switch_dashboard.storage.migration")


def migrate_legacy_data(metric_repo: MetricRepository, db=None) -> Dict[str, Any]:
    db = db or get_db()
    db.init_db()
    stats = {
        "counters_migrated": 0,
        "hourly_migrated": 0,
        "daily_migrated": 0,
        "scanner_migrated": 0,
    }

    # 1. Migrate counters.json
    if os.path.exists(COUNTERS_PATH):
        try:
            with open(COUNTERS_PATH, "r", encoding="utf-8") as f:
                counters = json.load(f)
            if isinstance(counters, dict) and counters:
                metric_repo.save_counters(counters)
                stats["counters_migrated"] = len(counters)
                logger.info(f"Migrated {len(counters)} counters from counters.json to SQLite.")
                # Backup old file
                shutil.move(COUNTERS_PATH, f"{COUNTERS_PATH}.bak")
        except Exception as e:
            logger.error(f"Error migrating {COUNTERS_PATH}: {e}")

    # 2. Migrate history_hourly.json
    if os.path.exists(HOURLY_PATH):
        try:
            with open(HOURLY_PATH, "r", encoding="utf-8") as f:
                raw_hourly = json.load(f)
            if isinstance(raw_hourly, dict):
                hourly_dict = {}
                for key_str, pts in raw_hourly.items():
                    if ":" in key_str and isinstance(pts, list):
                        parts = key_str.split(":", 1)
                        # Scrub potential spike anomalies
                        cleaned = [p for p in pts if p.get("tx", 0) <= 20_000_000_000 and p.get("rx", 0) <= 20_000_000_000]
                        hourly_dict[(parts[0], parts[1])] = cleaned
                        stats["hourly_migrated"] += len(cleaned)
                metric_repo.set_hourly_history_all(hourly_dict)
                logger.info(f"Migrated {stats['hourly_migrated']} hourly history points from {HOURLY_PATH}.")
                shutil.move(HOURLY_PATH, f"{HOURLY_PATH}.bak")
        except Exception as e:
            logger.error(f"Error migrating {HOURLY_PATH}: {e}")

    # 3. Migrate history_daily.json
    if os.path.exists(DAILY_PATH):
        try:
            with open(DAILY_PATH, "r", encoding="utf-8") as f:
                raw_daily = json.load(f)
            if isinstance(raw_daily, dict):
                daily_dict = {}
                for key_str, pts in raw_daily.items():
                    if ":" in key_str and isinstance(pts, list):
                        parts = key_str.split(":", 1)
                        cleaned = [p for p in pts if p.get("tx", 0) <= 20_000_000_000 and p.get("rx", 0) <= 20_000_000_000]
                        daily_dict[(parts[0], parts[1])] = cleaned
                        stats["daily_migrated"] += len(cleaned)
                metric_repo.set_daily_history_all(daily_dict)
                logger.info(f"Migrated {stats['daily_migrated']} daily history points from {DAILY_PATH}.")
                shutil.move(DAILY_PATH, f"{DAILY_PATH}.bak")
        except Exception as e:
            logger.error(f"Error migrating {DAILY_PATH}: {e}")

    # 4. Migrate standalone network_scanner.db if separate
    if os.path.exists(LEGACY_SCANNER_DB_PATH) and os.path.abspath(LEGACY_SCANNER_DB_PATH) != os.path.abspath(DATABASE_PATH):
        try:
            leg_conn = sqlite3.connect(LEGACY_SCANNER_DB_PATH)
            leg_conn.row_factory = sqlite3.Row
            leg_cur = leg_conn.cursor()

            with db.transaction() as target_cur:
                # Check if legacy hosts table exists
                leg_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='hosts';")
                if leg_cur.fetchone():
                    leg_cur.execute("SELECT * FROM hosts")
                    hosts = [dict(r) for r in leg_cur.fetchall()]
                    for h in hosts:
                        target_cur.execute("""
                            INSERT OR IGNORE INTO hosts (
                                ip_address, mac_address, vendor, hostname, ports, note,
                                status, known_host, first_seen, last_seen_online, last_updated
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            h["ip_address"], h.get("mac_address", ""), h.get("vendor", ""),
                            h.get("hostname", ""), h.get("ports", ""), h.get("note", ""),
                            h.get("status", "Offline"), h.get("known_host", 0),
                            h.get("first_seen", ""), h.get("last_seen_online", ""), h.get("last_updated", "")
                        ))
                    stats["scanner_migrated"] = len(hosts)

                # Migrate host_history
                leg_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='host_history';")
                if leg_cur.fetchone():
                    leg_cur.execute("SELECT * FROM host_history")
                    events = [dict(r) for r in leg_cur.fetchall()]
                    for ev in events:
                        target_cur.execute("""
                            INSERT OR IGNORE INTO host_history (ip_address, status, event_time)
                            VALUES (?, ?, ?)
                        """, (ev["ip_address"], ev["status"], ev["event_time"]))

            leg_conn.close()
            shutil.move(LEGACY_SCANNER_DB_PATH, f"{LEGACY_SCANNER_DB_PATH}.bak")
            logger.info(f"Migrated {stats['scanner_migrated']} hosts from legacy scanner db.")
        except Exception as e:
            logger.error(f"Error migrating legacy scanner db: {e}")

    return stats
