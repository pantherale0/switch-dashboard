import time
import logging
import threading
from collections import deque
from contextlib import contextmanager
from typing import Dict, List, Tuple, Any, Optional, Generator

from sqlalchemy import select, delete, or_, func, cast, Integer
from sqlalchemy.orm import Session

from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.engine import get_db_session
from switch_dashboard.storage.models.metrics import CounterBaseline, MetricHistory, MetricTrend
from switch_dashboard.core.ports import normalize_port

logger = logging.getLogger("switch_dashboard.storage.metric_repo")


class MetricRepository:
    def __init__(self, db=None, db_url: Optional[str] = None):
        self.db = db or get_db()
        self.db_url = db_url
        self._lock = threading.Lock()
        # High-frequency in-memory ring buffers for live graph feeds (sub-millisecond reads)
        self._history_live: Dict[Tuple[str, str], deque] = {}
        self._history_hourly: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._history_daily: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    @contextmanager
    def _get_session(self) -> Generator[Session, None, None]:
        if self.db and hasattr(self.db, "session"):
            with self.db.session() as session:
                yield session
        else:
            with get_db_session(self.db_url) as session:
                yield session

    def repair_negative_counters(self):
        """Repairs any legacy negative counter rows in counters table."""
        try:
            with self._get_session() as session:
                records = session.execute(
                    select(CounterBaseline).where(
                        or_(CounterBaseline.cum_tx < 0, CounterBaseline.cum_rx < 0)
                    )
                ).scalars().all()
                for r in records:
                    if r.cum_tx < 0:
                        r.cum_tx = max(0, r.tx_bytes)
                    if r.cum_rx < 0:
                        r.cum_rx = max(0, r.rx_bytes)
        except Exception as e:
            logger.debug(f"Could not repair counters table: {e}")

    def load_counters(self) -> Dict[str, Dict[str, Any]]:
        """Loads all counters from database into memory dictionary { 'ip:port': {...} }."""
        self.repair_negative_counters()
        counters = {}
        with self._get_session() as session:
            rows = session.execute(select(CounterBaseline)).scalars().all()
            for row in rows:
                key = f"{row.device_ip}:{normalize_port(row.port)}"
                tx_b = max(0, row.tx_bytes or 0)
                rx_b = max(0, row.rx_bytes or 0)
                cum_tx = row.cum_tx
                cum_rx = row.cum_rx
                if cum_tx is None or cum_tx < 0:
                    cum_tx = tx_b
                if cum_rx is None or cum_rx < 0:
                    cum_rx = rx_b
                counters[key] = {
                    "tx": tx_b,
                    "rx": rx_b,
                    "cum_tx": cum_tx,
                    "cum_rx": cum_rx,
                    "ts": row.timestamp,
                }
        return counters

    def save_counters(self, counters: Dict[str, Dict[str, Any]]):
        """Persists memory counters to the database in a single transaction."""
        with self._get_session() as session:
            for key, data in counters.items():
                if ":" in key:
                    parts = key.split(":", 1)
                    device_ip, port = parts[0], str(normalize_port(parts[1]))
                    tx_b = max(0, data.get("tx", 0))
                    rx_b = max(0, data.get("rx", 0))
                    cum_tx = max(0, data.get("cum_tx", 0))
                    cum_rx = max(0, data.get("cum_rx", 0))
                    ts = float(data.get("ts", time.time()))

                    baseline = session.get(CounterBaseline, (device_ip, port))
                    if baseline:
                        baseline.tx_bytes = tx_b
                        baseline.rx_bytes = rx_b
                        baseline.cum_tx = cum_tx
                        baseline.cum_rx = cum_rx
                        baseline.timestamp = ts
                    else:
                        session.add(CounterBaseline(
                            device_ip=device_ip,
                            port=port,
                            tx_bytes=tx_b,
                            rx_bytes=rx_b,
                            cum_tx=cum_tx,
                            cum_rx=cum_rx,
                            timestamp=ts,
                        ))

    def record_samples_batch(self, samples: List[Dict[str, Any]]):
        """Append-only batch insert for raw metric samples into database."""
        if not samples:
            return
        with self._get_session() as session:
            for s in samples:
                session.add(MetricHistory(
                    device_ip=s["device_ip"],
                    port=str(normalize_port(s["port"])),
                    timestamp=s["ts"],
                    tx_bytes=s.get("tx_bytes", 0),
                    rx_bytes=s.get("rx_bytes", 0),
                    speed_tx_bps=s.get("speed_tx_bps", 0),
                    speed_rx_bps=s.get("speed_rx_bps", 0),
                ))

    # In-memory live ring buffer management
    def append_live_sample(self, ip: str, port: Any, ts: float, cum_tx: int, cum_rx: int):
        key = (ip, normalize_port(port))
        with self._lock:
            if key not in self._history_live:
                self._history_live[key] = deque(maxlen=120)
            self._history_live[key].append({"ts": ts, "tx": cum_tx, "rx": cum_rx})

    def get_live_history(self, ip: str, port: Any) -> List[Dict[str, Any]]:
        key = (ip, normalize_port(port))
        with self._lock:
            return list(self._history_live.get(key, []))

    # Hourly history (high-res speed points in bps)
    def append_hourly_sample(self, ip: str, port: Any, ts: float, tx_bps: int, rx_bps: int, max_points: int = 120):
        key = (ip, normalize_port(port))
        with self._lock:
            if key not in self._history_hourly:
                self._history_hourly[key] = []
            pts = self._history_hourly[key]
            pts.append({"ts": ts, "tx": tx_bps, "rx": rx_bps})
            while len(pts) > max_points:
                pts.pop(0)

    def get_hourly_history(self, ip: str, port: Any) -> List[Dict[str, Any]]:
        key = (ip, normalize_port(port))
        with self._lock:
            return list(self._history_hourly.get(key, []))

    def set_hourly_history_all(self, hourly_dict: Dict[Tuple[str, str], List[Dict[str, Any]]]):
        with self._lock:
            self._history_hourly = {(k[0], normalize_port(k[1])): list(v) for k, v in hourly_dict.items()}

    def get_hourly_history_all(self) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
        with self._lock:
            return {k: list(v) for k, v in self._history_hourly.items()}

    # Daily history (15-min rolling average speed points)
    def append_daily_sample(self, ip: str, port: Any, ts: float, tx_bps: int, rx_bps: int, max_points: int = 96):
        key = (ip, normalize_port(port))
        with self._lock:
            if key not in self._history_daily:
                self._history_daily[key] = []
            pts = self._history_daily[key]
            pts.append({"ts": ts, "tx": tx_bps, "rx": rx_bps})
            while len(pts) > max_points:
                pts.pop(0)

    def get_daily_history(self, ip: str, port: Any) -> List[Dict[str, Any]]:
        key = (ip, normalize_port(port))
        with self._lock:
            return list(self._history_daily.get(key, []))

    def set_daily_history_all(self, daily_dict: Dict[Tuple[str, str], List[Dict[str, Any]]]):
        with self._lock:
            self._history_daily = {(k[0], normalize_port(k[1])): list(v) for k, v in daily_dict.items()}

    def get_daily_history_all(self) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
        with self._lock:
            return {k: list(v) for k, v in self._history_daily.items()}

    def get_avg_speed(self, ip: str, port: Any, window_sec: float = 900.0) -> Tuple[int, int]:
        pts = self.get_hourly_history(ip, port)
        if not pts:
            return 0, 0
        cutoff = time.time() - window_sec
        recent = [p for p in pts if p["ts"] >= cutoff]
        if not recent:
            return pts[-1]["tx"], pts[-1]["rx"]
        avg_tx = int(sum(p["tx"] for p in recent) / len(recent))
        avg_rx = int(sum(p["rx"] for p in recent) / len(recent))
        return avg_tx, avg_rx

    def get_history_range(self, ip: str, port: Any, range_type: str = "1h") -> Dict[str, List[Any]]:
        """
        Retrieves speed history (tx, rx in bps, timestamps) for a given port.
        Supports:
          - 'live': In-memory ring buffer deltas (smooth real-time updates) with fallback to last 10m from DB.
          - '1h': Raw metric_history samples over the past 3600 seconds from DB (fallback to in-memory).
          - '24h': 15-minute bucketed averages over the past 86400 seconds from DB (fallback to in-memory).
        """
        norm_port = str(normalize_port(port))
        now = time.time()
        tx: List[int] = []
        rx: List[int] = []
        timestamps: List[float] = []

        if range_type == "live":
            hl = self.get_live_history(ip, port)
            if len(hl) >= 2:
                for i in range(1, len(hl)):
                    dt = hl[i]["ts"] - hl[i - 1]["ts"]
                    if dt > 0:
                        tx_diff = hl[i]["tx"] - hl[i - 1]["tx"]
                        rx_diff = hl[i]["rx"] - hl[i - 1]["rx"]
                        if tx_diff < 0:
                            tx_diff = hl[i]["tx"]
                        if rx_diff < 0:
                            rx_diff = hl[i]["rx"]
                        tx.append(int(tx_diff * 8 / dt))
                        rx.append(int(rx_diff * 8 / dt))
                        timestamps.append(hl[i]["ts"])
                return {"tx": tx, "rx": rx, "timestamps": timestamps}

            # Fallback for fresh boot when in-memory ring buffer has < 2 samples: read last 10 mins from DB
            cutoff = now - 600
            try:
                with self._get_session() as session:
                    stmt = (
                        select(MetricHistory.timestamp, MetricHistory.speed_tx_bps, MetricHistory.speed_rx_bps)
                        .where(
                            MetricHistory.device_ip == ip,
                            MetricHistory.port == norm_port,
                            MetricHistory.timestamp >= cutoff,
                        )
                        .order_by(MetricHistory.timestamp.asc())
                    )
                    for r in session.execute(stmt).all():
                        timestamps.append(r[0])
                        tx.append(r[1])
                        rx.append(r[2])
            except Exception as e:
                logger.error(f"Error reading live history from database: {e}")

            return {"tx": tx, "rx": rx, "timestamps": timestamps}

        elif range_type == "1h":
            cutoff = now - 3600
            try:
                with self._get_session() as session:
                    stmt = (
                        select(MetricHistory.timestamp, MetricHistory.speed_tx_bps, MetricHistory.speed_rx_bps)
                        .where(
                            MetricHistory.device_ip == ip,
                            MetricHistory.port == norm_port,
                            MetricHistory.timestamp >= cutoff,
                        )
                        .order_by(MetricHistory.timestamp.asc())
                    )
                    for r in session.execute(stmt).all():
                        timestamps.append(r[0])
                        tx.append(r[1])
                        rx.append(r[2])
            except Exception as e:
                logger.error(f"Error querying 1h history from database: {e}")

            if not tx:
                # In-memory fallback
                for p in self.get_hourly_history(ip, port):
                    tx.append(p.get("tx", 0))
                    rx.append(p.get("rx", 0))
                    timestamps.append(p.get("ts", 0))

            return {"tx": tx, "rx": rx, "timestamps": timestamps}

        elif range_type == "24h":
            cutoff = now - 86400
            try:
                with self._get_session() as session:
                    bucket_col = cast(MetricHistory.timestamp / 900, Integer) * 900
                    stmt = (
                        select(
                            bucket_col.label("bucket_ts"),
                            func.avg(MetricHistory.speed_tx_bps).label("avg_tx"),
                            func.avg(MetricHistory.speed_rx_bps).label("avg_rx"),
                        )
                        .where(
                            MetricHistory.device_ip == ip,
                            MetricHistory.port == norm_port,
                            MetricHistory.timestamp >= cutoff,
                        )
                        .group_by(bucket_col)
                        .order_by(bucket_col.asc())
                    )
                    for r in session.execute(stmt).all():
                        timestamps.append(r[0])
                        tx.append(int(r[1] or 0))
                        rx.append(int(r[2] or 0))
            except Exception as e:
                logger.error(f"Error querying 24h history from database: {e}")

            if not tx:
                # In-memory fallback
                for p in self.get_daily_history(ip, port):
                    tx.append(p.get("tx", 0))
                    rx.append(p.get("rx", 0))
                    timestamps.append(p.get("ts", 0))

            return {"tx": tx, "rx": rx, "timestamps": timestamps}

        return {"tx": tx, "rx": rx, "timestamps": timestamps}

    def reset_all_counters(self):
        with self._lock:
            self._history_live.clear()
            self._history_hourly.clear()
            self._history_daily.clear()
        with self._get_session() as session:
            session.execute(delete(CounterBaseline))
            session.execute(delete(MetricHistory))
            session.execute(delete(MetricTrend))


_metric_repo: Optional[MetricRepository] = None


def get_metric_repo(db=None) -> MetricRepository:
    global _metric_repo
    if db is not None:
        return MetricRepository(db)
    if _metric_repo is None:
        _metric_repo = MetricRepository()
    return _metric_repo
