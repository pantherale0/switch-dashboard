from sqlalchemy import Column, Integer, String, Text
from switch_dashboard.storage.models.base import Base


class ScannerHost(Base):
    """Subnet scanner detected active hosts."""
    __tablename__ = "hosts"

    ip_address = Column(String(64), primary_key=True)
    mac_address = Column(String(32), default="")
    vendor = Column(String(255), default="")
    hostname = Column(String(255), default="")
    ports = Column(Text, default="")
    note = Column(Text, default="")
    status = Column(String(32), default="")
    known_host = Column(Integer, default=0)
    first_seen = Column(String(64), default="")
    last_seen_online = Column(String(64), default="")
    last_updated = Column(String(64), default="")


class ScannerHostHistory(Base):
    """Scanner host online/offline state change events."""
    __tablename__ = "host_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ip_address = Column(String(64), nullable=False, index=True)
    status = Column(Integer, default=0)
    event_time = Column(String(64), default="")
