import time
import logging
import threading
from contextlib import contextmanager
from typing import Optional, Generator

from sqlalchemy import select, delete, func, cast, Integer
from sqlalchemy.orm import Session

from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.engine import get_db_session
from switch_dashboard.storage.models.metrics import MetricHistory, MetricTrend
from switch_dashboard.storage.models.client_monitoring import ClientConnectionHistory, ClientMetricTrend
from switch_dashboard.config import get_setting

logger = logging.getLogger("switch_dashboard.storage.housekeeper")


class Housekeeper:
    """Zabbix-inspired Housekeeper process.

    Periodically aggregates raw metric samples into 1-hour trend rollups
    and purges expired raw history and trends.
    """

    def __init__(self, db=None, db_url: Optional[str] = None):
        self.db = db or get_db()
        self.db_url = db_url
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        if self.db and hasattr(self.db, "session"):
            with self.db.session() as session:
                yield session
        else:
            with get_db_session(self.db_url) as session:
                yield session

    def run_once(self) -> dict:
        now = time.time()
        logger.info("Starting Housekeeper maintenance cycle...")

        # 1. Rollup raw samples into hourly trends (for samples older than 2 hours)
        rollup_cutoff = now - 7200
        stats = {
            "trends_aggregated": 0,
            "raw_purged": 0,
            "trends_purged": 0,
        }

        with self._get_session() as session:
            # Aggregate hourly buckets: floor(timestamp / 3600) * 3600
            hour_bucket = cast(MetricHistory.timestamp / 3600, Integer) * 3600
            stmt = (
                select(
                    MetricHistory.device_ip,
                    MetricHistory.port,
                    hour_bucket.label("hour_ts"),
                    func.count().label("sample_count"),
                    func.cast(func.avg(MetricHistory.speed_tx_bps), Integer).label("avg_tx_bps"),
                    func.max(MetricHistory.speed_tx_bps).label("max_tx_bps"),
                    func.min(MetricHistory.speed_tx_bps).label("min_tx_bps"),
                    func.cast(func.avg(MetricHistory.speed_rx_bps), Integer).label("avg_rx_bps"),
                    func.max(MetricHistory.speed_rx_bps).label("max_rx_bps"),
                    func.min(MetricHistory.speed_rx_bps).label("min_rx_bps"),
                )
                .where(MetricHistory.timestamp <= rollup_cutoff)
                .group_by(MetricHistory.device_ip, MetricHistory.port, hour_bucket)
            )
            aggregated = session.execute(stmt).all()
            for row in aggregated:
                device_ip, port, hour_ts, sample_count, avg_tx, max_tx, min_tx, avg_rx, max_rx, min_rx = row
                existing = session.execute(
                    select(MetricTrend).where(
                        MetricTrend.device_ip == device_ip,
                        MetricTrend.port == port,
                        MetricTrend.hour_timestamp == hour_ts,
                    )
                ).scalar_one_or_none()
                if existing:
                    existing.sample_count = sample_count
                    existing.avg_tx_bps = avg_tx or 0
                    existing.max_tx_bps = max_tx or 0
                    existing.min_tx_bps = min_tx or 0
                    existing.avg_rx_bps = avg_rx or 0
                    existing.max_rx_bps = max_rx or 0
                    existing.min_rx_bps = min_rx or 0
                else:
                    session.add(MetricTrend(
                        device_ip=device_ip,
                        port=port,
                        hour_timestamp=hour_ts,
                        sample_count=sample_count,
                        avg_tx_bps=avg_tx or 0,
                        max_tx_bps=max_tx or 0,
                        min_tx_bps=min_tx or 0,
                        avg_rx_bps=avg_rx or 0,
                        max_rx_bps=max_rx or 0,
                        min_rx_bps=min_rx or 0,
                    ))
            stats["trends_aggregated"] = len(aggregated)

            # 2. Purge raw samples older than retention window (default: 7 days)
            raw_retention_days = int(get_setting("history_retention_days", 7))
            raw_purge_cutoff = now - (raw_retention_days * 86400)
            res_raw = session.execute(delete(MetricHistory).where(MetricHistory.timestamp < raw_purge_cutoff))
            stats["raw_purged"] = res_raw.rowcount if res_raw.rowcount is not None and res_raw.rowcount >= 0 else 0

            # 3. Purge trend records older than long-term retention (default: 90 days)
            trends_retention_days = int(get_setting("trends_retention_days", 90))
            trends_purge_cutoff = now - (trends_retention_days * 86400)
            res_trends = session.execute(delete(MetricTrend).where(MetricTrend.hour_timestamp < trends_purge_cutoff))
            stats["trends_purged"] = res_trends.rowcount if res_trends.rowcount is not None and res_trends.rowcount >= 0 else 0

            # 4. Purge client connection history and metric trends older than 90 days
            try:
                session.execute(delete(ClientConnectionHistory).where(ClientConnectionHistory.connected_at < trends_purge_cutoff))
                session.execute(delete(ClientMetricTrend).where(ClientMetricTrend.hour_timestamp < trends_purge_cutoff))
            except Exception as e:
                logger.debug(f"Housekeeper client history cleanup notice: {e}")

        # 4. Optimize SQLite database if supported
        try:
            if hasattr(self.db, "connect"):
                with self.db.connect() as conn:
                    conn.execute("PRAGMA optimize;")
        except Exception as e:
            logger.debug(f"Housekeeper optimize note: {e}")

        logger.info(
            f"Housekeeper cycle complete: {stats['trends_aggregated']} trend buckets updated, "
            f"{stats['raw_purged']} raw samples purged, {stats['trends_purged']} old trends purged."
        )
        return stats

    def start(self, interval_seconds: int = 3600):
        if self._thread and self._thread.is_alive():
            return

        def _loop():
            logger.info("Housekeeper daemon started.")
            # Run initial check after 60 seconds
            if not self._stop_event.wait(60):
                try:
                    self.run_once()
                except Exception as e:
                    logger.error(f"Housekeeper error: {e}")

            while not self._stop_event.wait(interval_seconds):
                try:
                    self.run_once()
                except Exception as e:
                    logger.error(f"Housekeeper error: {e}")

        self._stop_event.clear()
        self._thread = threading.Thread(target=_loop, daemon=True, name="HousekeeperThread")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
