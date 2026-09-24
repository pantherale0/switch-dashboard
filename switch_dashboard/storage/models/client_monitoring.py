from sqlalchemy import BigInteger, Column, Float, Index, Integer, String, Text
from switch_dashboard.storage.models.base import Base


class ClientIpHistory(Base):
    """Historical record of all IP addresses associated with each MAC address."""
    __tablename__ = "client_ip_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mac = Column(String(32), nullable=False, index=True)
    ip = Column(String(64), nullable=False, index=True)
    hostname = Column(String(255), default="")
    first_seen = Column(Float, nullable=False)
    last_seen = Column(Float, nullable=False)
    is_active = Column(Integer, default=1)

    __table_args__ = (
        Index("idx_client_ip_mac_active", "mac", "is_active"),
        Index("idx_client_ip_lookup", "mac", "ip"),
    )


class ClientConnectionHistory(Base):
    """Session history and roaming/connection events for clients."""
    __tablename__ = "client_connection_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mac = Column(String(32), nullable=False, index=True)
    event_type = Column(String(32), nullable=False)  # 'connected', 'disconnected', 'roamed'
    switch_ip = Column(String(64), nullable=False)
    switch_name = Column(String(255), default="")
    port = Column(String(64), nullable=False)
    vlan = Column(String(32), default="1")
    ssid = Column(String(128), default="")
    signal_dbm = Column(Integer, nullable=True)
    from_switch_ip = Column(String(64), nullable=True)
    from_port = Column(String(64), nullable=True)
    connected_at = Column(Float, nullable=False)
    disconnected_at = Column(Float, nullable=True)

    __table_args__ = (
        Index("idx_client_conn_mac_time", "mac", "connected_at"),
        Index("idx_client_conn_event", "event_type", "connected_at"),
    )


class ClientMetricTrend(Base):
    """Hourly aggregated bandwidth volume and peak rates per client."""
    __tablename__ = "client_metric_trends"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mac = Column(String(32), nullable=False)
    hour_timestamp = Column(Integer, nullable=False)
    tx_bytes = Column(BigInteger, nullable=False, default=0)
    rx_bytes = Column(BigInteger, nullable=False, default=0)
    max_tx_bps = Column(BigInteger, nullable=False, default=0)
    max_rx_bps = Column(BigInteger, nullable=False, default=0)
    avg_tx_bps = Column(BigInteger, nullable=False, default=0)
    avg_rx_bps = Column(BigInteger, nullable=False, default=0)
    sample_count = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index("idx_client_trend_mac_hour", "mac", "hour_timestamp", unique=True),
    )
