from sqlalchemy import BigInteger, Column, Float, Index, Integer, String, Text, UniqueConstraint
from switch_dashboard.storage.models.base import Base


class CounterBaseline(Base):
    """Real-time counter baselines and cumulative traffic totals per device port."""
    __tablename__ = "counters"

    device_ip = Column(String(64), primary_key=True)
    port = Column(String(64), primary_key=True)
    tx_bytes = Column(BigInteger, nullable=False, default=0)
    rx_bytes = Column(BigInteger, nullable=False, default=0)
    cum_tx = Column(BigInteger, nullable=False, default=0)
    cum_rx = Column(BigInteger, nullable=False, default=0)
    timestamp = Column(Float, nullable=False)


class MetricHistory(Base):
    """Raw high-resolution time-series metric samples."""
    __tablename__ = "metric_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_ip = Column(String(64), nullable=False)
    port = Column(String(64), nullable=False)
    timestamp = Column(Float, nullable=False)
    tx_bytes = Column(BigInteger, nullable=False)
    rx_bytes = Column(BigInteger, nullable=False)
    speed_tx_bps = Column(BigInteger, nullable=False, default=0)
    speed_rx_bps = Column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        Index("idx_metric_hist", "device_ip", "port", "timestamp"),
    )


class MetricTrend(Base):
    """Pre-aggregated hourly traffic trends (min, max, avg)."""
    __tablename__ = "metric_trends"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_ip = Column(String(64), nullable=False)
    port = Column(String(64), nullable=False)
    hour_timestamp = Column(Integer, nullable=False)
    sample_count = Column(Integer, nullable=False)
    avg_tx_bps = Column(BigInteger, nullable=False)
    max_tx_bps = Column(BigInteger, nullable=False)
    min_tx_bps = Column(BigInteger, nullable=False)
    avg_rx_bps = Column(BigInteger, nullable=False)
    max_rx_bps = Column(BigInteger, nullable=False)
    min_rx_bps = Column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("device_ip", "port", "hour_timestamp", name="uq_metric_trend_device_port_hour"),
        Index("idx_metric_trends", "device_ip", "port", "hour_timestamp"),
    )


class DeviceStateCache(Base):
    """Cached full telemetry and cluster snapshot per device for warm boot."""
    __tablename__ = "device_state_cache"

    device_ip = Column(String(64), primary_key=True)
    data_json = Column(Text, nullable=False)
    speeds_json = Column(Text, default="{}")
    updated_at = Column(Float, nullable=False)
