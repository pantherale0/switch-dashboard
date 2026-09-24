from sqlalchemy import Column, Float, String
from switch_dashboard.storage.models.base import Base


class MacEntry(Base):
    """Switch forwarding database (MAC address table) learned entries."""
    __tablename__ = "mac_entries"

    device_ip = Column(String(64), primary_key=True)
    port = Column(String(64), primary_key=True)
    mac = Column(String(32), primary_key=True)
    vlan = Column(String(32), default="1")
    last_seen = Column(Float, nullable=False)
