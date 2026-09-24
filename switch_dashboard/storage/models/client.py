from sqlalchemy import Column, Float, Integer, String
from switch_dashboard.storage.models.base import Base


class DiscoveredClient(Base):
    """Network clients discovered passively or actively from switch/AP tables,

    hypervisors, ARP caches, or scanner runs.
    """
    __tablename__ = "discovered_clients"

    mac = Column(String(32), primary_key=True)
    ip = Column(String(64), default="", index=True)
    hostname = Column(String(255), default="")
    custom_name = Column(String(255), default="")
    vendor = Column(String(255), default="")
    device_type = Column(String(64), default="client")
    switch_ip = Column(String(64), default="")
    port = Column(String(64), default="")
    vlan = Column(String(32), default="1")
    first_seen = Column(Float, nullable=False, default=0.0)
    last_seen = Column(Float, nullable=False, default=0.0)
    status = Column(String(32), default="online")
    is_mini_switch = Column(Integer, default=0)
    passthrough_port = Column(String(64), default="PC")
    ssid = Column(String(128), default="")
    signal_dbm = Column(Integer, nullable=True)
