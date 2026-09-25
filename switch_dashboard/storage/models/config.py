from sqlalchemy import Boolean, Column, Float, Integer, String, Text
from switch_dashboard.storage.models.base import Base


class ConfigSetting(Base):
    """Stores global application settings, feature flags, custom vendors, etc.

    as JSON documents keyed by setting name.
    """
    __tablename__ = "config_settings"

    key = Column(String(255), primary_key=True)
    value_json = Column(Text, nullable=False, default="{}")
    updated_at = Column(Float, nullable=False, default=0.0)


class DeviceConfig(Base):
    """Stores configured network devices (managed switches, distribution/core switches,

    routers, access points, hypervisors, unmanaged switches, and desk phones).
    """
    __tablename__ = "devices"

    id = Column(String(128), primary_key=True)
    name = Column(String(255), nullable=False)
    ip = Column(String(64), unique=True, nullable=False, index=True)
    model = Column(String(128), default="")
    protocol = Column(String(64), default="http_hc")
    device_type = Column(String(64), default="switch")
    role = Column(String(64), default="switch")
    management_type = Column(String(64), default="managed")  # 'managed' or 'unmanaged'
    port_count = Column(Integer, default=8)
    enabled = Column(Boolean, default=True)
    username = Column(String(128), default="admin")
    password = Column(String(255), default="")
    community = Column(String(128), default="public")
    snmp_version = Column(String(16), default="2c")
    parent_ip = Column(String(64), default="")
    parent_port = Column(String(64), default="")
    uplink_port = Column(String(64), default="")
    config_json = Column(Text, default="{}")  # Extra attributes (e.g. proxmox, unifi nodes)
    last_seen = Column(Float, default=0.0)
    status = Column(String(32), default="unknown")


class DeviceSecret(Base):
    """Authenticated encrypted credentials for one managed device."""
    __tablename__ = "device_secrets"

    device_id = Column(String(128), primary_key=True)
    key_id = Column(String(64), nullable=False)
    ciphertext = Column(Text, nullable=False)
    updated_at = Column(Float, nullable=False, default=0.0)


class PortNote(Base):
    """Custom user notes and annotations for switch and device ports."""
    __tablename__ = "port_notes"

    device_ip = Column(String(64), primary_key=True)
    port = Column(String(64), primary_key=True)
    note = Column(Text, default="")
    updated_at = Column(Float, default=0.0)


class ClientOverride(Base):
    """User-configured client overrides (custom nickname, device type,

    passthrough mini-switch mode).
    """
    __tablename__ = "client_overrides"

    mac = Column(String(32), primary_key=True)  # Normalized 12-char hex MAC
    host = Column(String(255), default="")
    device_type = Column(String(64), default="client")
    is_mini_switch = Column(Boolean, default=False)
    passthrough_port = Column(String(64), default="PC")
    updated_at = Column(Float, default=0.0)
