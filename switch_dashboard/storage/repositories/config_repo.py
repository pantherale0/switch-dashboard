import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from switch_dashboard.storage.engine import get_db_session
from switch_dashboard.storage.models.client import DiscoveredClient
from switch_dashboard.storage.models.config import (
    ClientOverride,
    ConfigSetting,
    DeviceConfig,
    DeviceSecret,
    PortNote,
)
from switch_dashboard.security.crypto import decrypt_json, encrypt_json, redact_tree, split_secrets

logger = logging.getLogger("switch_dashboard.storage.repositories.config_repo")


class ConfigRepository:
    """SQLAlchemy repository for application configuration persistence in the database."""

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url

    def has_any_config(self) -> bool:
        """Returns True if the database contains any configured devices or settings."""
        with get_db_session(self.db_url) as session:
            has_devices = session.scalar(select(DeviceConfig.id).limit(1)) is not None
            has_settings = session.scalar(select(ConfigSetting.key).limit(1)) is not None
            return bool(has_devices or has_settings)

    def import_from_json_if_empty(self, json_path: str) -> bool:
        """If the database is empty and a legacy JSON configuration file exists,

        imports all devices, settings, clients, and notes into the database.
        """
        if not os.path.exists(json_path):
            return False

        if self.has_any_config():
            return False

        logger.info(f"Database config is empty. Migrating legacy configuration from {json_path}...")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            self.save_full_config(cfg)
            logger.info("Successfully imported legacy configuration into the database.")

            sanitized = redact_tree(cfg)
            temp_path = f"{json_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(sanitized, f, indent=2)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, json_path)
            return True
        except Exception as e:
            logger.error(f"Failed to import legacy config from {json_path}: {e}", exc_info=True)
            from switch_dashboard.security.crypto import CryptoConfigurationError
            if isinstance(e, CryptoConfigurationError):
                raise
            return False

    def load_full_config(self) -> Dict[str, Any]:
        """Loads and synthesizes the full configuration dictionary from database models."""
        cfg: Dict[str, Any] = {
            "devices": [],
            "switches": [],
            "access_points": [],
            "unmanaged_switches": [],
            "infrastructure_devices": [],
            "proxmox_nodes": [],
            "unifi_nodes": [],
            "clients": {},
            "port_notes": {},
            "notes": {},
            "settings": {},
        }

        with get_db_session(self.db_url) as session:
            encrypted_secrets = {secret.device_id: secret for secret in session.scalars(select(DeviceSecret))}
            # 1. Load Config Settings
            for setting in session.scalars(select(ConfigSetting)):
                try:
                    val = json.loads(setting.value_json)
                    if setting.key == "settings":
                        cfg["settings"] = val
                    elif setting.key == "proxmox_nodes":
                        cfg["proxmox_nodes"] = val
                    elif setting.key == "unifi_nodes":
                        cfg["unifi_nodes"] = val
                    else:
                        cfg[setting.key] = val
                except Exception:
                    pass

            # 2. Load Devices
            devices_list: List[Dict[str, Any]] = []
            for dev in session.scalars(select(DeviceConfig).order_by(DeviceConfig.name)):
                d_dict: Dict[str, Any] = {
                    "id": dev.id,
                    "name": dev.name,
                    "ip": "" if (dev.ip and dev.ip.startswith("unmanaged_")) else dev.ip,
                    "model": dev.model,
                    "protocol": dev.protocol,
                    "device_type": dev.device_type,
                    "role": dev.role,
                    "management_type": dev.management_type,
                    "port_count": dev.port_count,
                    "enabled": dev.enabled,
                    "username": dev.username,
                    "password": dev.password,
                    "community": dev.community,
                    "snmp_version": dev.snmp_version,
                    "parent_ip": dev.parent_ip,
                    "parent_port": dev.parent_port,
                    "uplink_port": dev.uplink_port,
                    "last_seen": dev.last_seen,
                    "status": dev.status,
                }
                if dev.config_json:
                    try:
                        extra = json.loads(dev.config_json)
                        if isinstance(extra, dict):
                            for k, v in extra.items():
                                d_dict.setdefault(k, v)
                    except Exception:
                        pass
                legacy_public, legacy_extra_secrets = split_secrets(d_dict)
                legacy_secrets = dict(legacy_extra_secrets)
                if dev.password:
                    legacy_secrets["password"] = dev.password
                if dev.community and dev.community != "public":
                    legacy_secrets["community"] = dev.community

                encrypted = encrypted_secrets.get(dev.id)
                if encrypted:
                    legacy_secrets.update(decrypt_json(encrypted.ciphertext, f"device:{dev.id}"))
                elif legacy_secrets:
                    key_id, ciphertext = encrypt_json(legacy_secrets, f"device:{dev.id}")
                    session.add(DeviceSecret(
                        device_id=dev.id,
                        key_id=key_id,
                        ciphertext=ciphertext,
                        updated_at=time.time(),
                    ))
                    dev.password = ""
                    dev.community = ""
                    dev.config_json = json.dumps(legacy_public)

                d_dict = legacy_public
                d_dict.update(legacy_secrets)
                devices_list.append(d_dict)

            cfg["devices"] = devices_list

            # Synthesize legacy arrays for full backward compatibility
            switches = []
            unmanaged = []
            infra = []
            aps = []
            for d in devices_list:
                m_type = d.get("management_type", "managed")
                d_type = d.get("device_type", "switch")
                role = d.get("role", "switch")

                if d_type == "access_point" or role == "access_point":
                    aps.append(dict(d))
                elif m_type == "managed":
                    switches.append(dict(d))
                elif d_type in ["unmanaged_switch", "phone", "ont"] or role in ["unmanaged_switch", "phone", "ont"]:
                    unmanaged.append(dict(d))
                else:
                    infra.append(dict(d))

            cfg["switches"] = switches
            cfg["access_points"] = aps
            cfg["unmanaged_switches"] = unmanaged
            cfg["infrastructure_devices"] = infra

            # 3. Load Port Notes
            notes_dict: Dict[str, str] = {}
            for pn in session.scalars(select(PortNote)):
                key = f"{pn.device_ip}:{pn.port}"
                notes_dict[key] = pn.note or ""
                # Also include "Port X" and raw number forms
                if not str(pn.port).startswith("Port "):
                    notes_dict[f"{pn.device_ip}:Port {pn.port}"] = pn.note or ""

            cfg["port_notes"] = notes_dict
            cfg["notes"] = notes_dict

            # 4. Load Client Overrides
            clients_dict: Dict[str, Dict[str, Any]] = {}
            for co in session.scalars(select(ClientOverride)):
                clean_mac = co.mac.replace(":", "").replace("-", "").upper()
                formatted_mac = ":".join(clean_mac[i : i + 2] for i in range(0, len(clean_mac), 2))
                clients_dict[clean_mac] = {
                    "mac": formatted_mac,
                    "host": co.host or "",
                    "device_type": co.device_type or "client",
                    "is_mini_switch": bool(co.is_mini_switch),
                    "passthrough_port": co.passthrough_port or "PC",
                }

            # Also incorporate any DiscoveredClient custom nicknames/mini_switch settings
            for dc in session.scalars(select(DiscoveredClient)):
                clean_mac = dc.mac.replace(":", "").replace("-", "").upper()
                if clean_mac not in clients_dict:
                    if dc.custom_name or dc.is_mini_switch:
                        clients_dict[clean_mac] = {
                            "mac": dc.mac,
                            "host": dc.custom_name or "",
                            "device_type": dc.device_type or "client",
                            "is_mini_switch": bool(dc.is_mini_switch),
                            "passthrough_port": dc.passthrough_port or "PC",
                        }

            cfg["clients"] = clients_dict

        return cfg

    def save_full_config(self, cfg: Dict[str, Any]) -> bool:
        """Persists the full configuration dictionary into the database."""
        now = time.time()
        with get_db_session(self.db_url) as session:
            # 1. Persist Config Settings
            settings_payload = cfg.get("settings", {})
            self._upsert_setting(session, "settings", settings_payload, now)

            if "proxmox_nodes" in cfg:
                self._upsert_setting(session, "proxmox_nodes", redact_tree(cfg["proxmox_nodes"]), now)
            if "unifi_nodes" in cfg:
                self._upsert_setting(session, "unifi_nodes", redact_tree(cfg["unifi_nodes"]), now)
            if "custom_vendors" in cfg:
                self._upsert_setting(session, "custom_vendors", cfg["custom_vendors"], now)
            if "ignored_macs" in cfg:
                self._upsert_setting(session, "ignored_macs", cfg["ignored_macs"], now)

            # 2. Persist Unified Devices
            raw_devices = cfg.get("devices")
            if raw_devices is None or not isinstance(raw_devices, list):
                # Synthesize from legacy arrays if "devices" was not explicitly provided
                raw_devices = []
                for s in cfg.get("switches", []):
                    item = dict(s)
                    item.setdefault("management_type", "managed")
                    raw_devices.append(item)
                for a in cfg.get("access_points", []):
                    item = dict(a)
                    item.setdefault("management_type", "managed")
                    item.setdefault("device_type", "access_point")
                    raw_devices.append(item)
                for u in cfg.get("unmanaged_switches", []):
                    item = dict(u)
                    item.setdefault("management_type", "unmanaged")
                    item.setdefault("device_type", "unmanaged_switch")
                    raw_devices.append(item)
                for i in cfg.get("infrastructure_devices", []):
                    item = dict(i)
                    item.setdefault("management_type", "unmanaged")
                    raw_devices.append(item)

            active_device_ips = set()
            active_device_ids = set()
            for idx, d in enumerate(raw_devices):
                public_device, submitted_secrets = split_secrets(d)
                clear_fields = public_device.get("clear_secrets", [])
                public_device.pop("clear_secrets", None)
                for field in list(public_device):
                    if field.endswith("_configured"):
                        public_device.pop(field)
                d = public_device
                dev_id = str(d.get("id") or f"dev_{idx}").strip()
                active_device_ids.add(dev_id)
                ip = str(d.get("ip") or "").strip()
                if not ip:
                    ip = f"unmanaged_{dev_id}"
                active_device_ips.add(ip)
                name = str(d.get("name") or ip).strip()

                extra_attrs = {
                    k: v
                    for k, v in d.items()
                    if k
                    not in [
                        "id",
                        "name",
                        "ip",
                        "model",
                        "protocol",
                        "device_type",
                        "role",
                        "management_type",
                        "port_count",
                        "enabled",
                        "username",
                        "password",
                        "community",
                        "snmp_version",
                        "parent_ip",
                        "parent_port",
                        "uplink_port",
                        "last_seen",
                        "status",
                    ]
                }

                existing_dev = session.scalar(
                    select(DeviceConfig).where((DeviceConfig.id == dev_id) | (DeviceConfig.ip == ip))
                )
                if existing_dev:
                    existing_dev.name = name
                    existing_dev.model = str(d.get("model", existing_dev.model))
                    existing_dev.protocol = str(d.get("protocol", existing_dev.protocol))
                    existing_dev.device_type = str(d.get("device_type", existing_dev.device_type))
                    existing_dev.role = str(d.get("role", existing_dev.role))
                    existing_dev.management_type = str(d.get("management_type", existing_dev.management_type))
                    existing_dev.port_count = int(d.get("port_count", existing_dev.port_count or 8))
                    existing_dev.enabled = bool(d.get("enabled", True))
                    if "username" in d:
                        existing_dev.username = str(d["username"])
                    existing_dev.password = ""
                    existing_dev.community = ""
                    if "snmp_version" in d:
                        existing_dev.snmp_version = str(d["snmp_version"])
                    if "parent_ip" in d:
                        existing_dev.parent_ip = str(d["parent_ip"])
                    if "parent_port" in d:
                        existing_dev.parent_port = str(d["parent_port"])
                    if "uplink_port" in d:
                        existing_dev.uplink_port = str(d["uplink_port"])
                    if extra_attrs:
                        existing_dev.config_json = json.dumps(extra_attrs)
                else:
                    new_dev = DeviceConfig(
                        id=dev_id,
                        name=name,
                        ip=ip,
                        model=str(d.get("model") or ""),
                        protocol=str(d.get("protocol", "http_hc")),
                        device_type=str(d.get("device_type", "switch")),
                        role=str(d.get("role", "switch")),
                        management_type=str(d.get("management_type", "managed")),
                        port_count=int(d.get("port_count", 8)),
                        enabled=bool(d.get("enabled", True)),
                        username=str(d.get("username", "admin")),
                        password="",
                        community="",
                        snmp_version=str(d.get("snmp_version", "2c")),
                        parent_ip=str(d.get("parent_ip", "")),
                        parent_port=str(d.get("parent_port", "")),
                        uplink_port=str(d.get("uplink_port", "")),
                        config_json=json.dumps(extra_attrs),
                    )
                    session.add(new_dev)

                existing_secret = session.get(DeviceSecret, dev_id)
                current_secrets = {}
                if existing_secret:
                    current_secrets = decrypt_json(existing_secret.ciphertext, f"device:{dev_id}")
                current_secrets.update(submitted_secrets)
                if isinstance(clear_fields, list):
                    for field in clear_fields:
                        if str(field) in submitted_secrets or str(field) in current_secrets:
                            current_secrets.pop(str(field), None)
                if current_secrets:
                    key_id, ciphertext = encrypt_json(current_secrets, f"device:{dev_id}")
                    if existing_secret:
                        existing_secret.key_id = key_id
                        existing_secret.ciphertext = ciphertext
                        existing_secret.updated_at = now
                    else:
                        session.add(DeviceSecret(
                            device_id=dev_id,
                            key_id=key_id,
                            ciphertext=ciphertext,
                            updated_at=now,
                        ))
                elif existing_secret:
                    session.delete(existing_secret)

            # Delete devices that were removed from the configuration
            if active_device_ids:
                session.execute(delete(DeviceConfig).where(DeviceConfig.id.not_in(active_device_ids)))
                session.execute(delete(DeviceSecret).where(DeviceSecret.device_id.not_in(active_device_ids)))
            else:
                session.execute(delete(DeviceConfig))
                session.execute(delete(DeviceSecret))

            # 3. Persist Port Notes
            notes_payload = cfg.get("port_notes") or cfg.get("notes") or {}
            seen_notes = set()
            for key, note_text in notes_payload.items():
                if ":" in str(key):
                    p_ip, p_num = str(key).split(":", 1)
                    p_num = p_num.replace("Port ", "").strip()
                    pair = (p_ip, p_num)
                    if not p_num or pair in seen_notes:
                        continue
                    seen_notes.add(pair)
                    existing_pn = session.scalar(
                        select(PortNote).where(PortNote.device_ip == p_ip, PortNote.port == p_num)
                    )
                    if existing_pn:
                        existing_pn.note = str(note_text)
                        existing_pn.updated_at = now
                    else:
                        session.add(
                            PortNote(
                                device_ip=p_ip,
                                port=p_num,
                                note=str(note_text),
                                updated_at=now,
                            )
                        )

            # 4. Persist Client Overrides
            clients_payload = cfg.get("clients", {})
            for raw_mac, c_info in clients_payload.items():
                clean_mac = str(raw_mac).replace(":", "").replace("-", "").upper()
                if not clean_mac or len(clean_mac) != 12:
                    continue

                host = str(c_info.get("host") or c_info.get("name") or "").strip()
                dev_type = str(c_info.get("device_type") or "client").strip()
                is_mini = bool(c_info.get("is_mini_switch", False))
                passthrough_p = str(c_info.get("passthrough_port") or "PC").strip()

                existing_co = session.scalar(select(ClientOverride).where(ClientOverride.mac == clean_mac))
                if existing_co:
                    existing_co.host = host
                    existing_co.device_type = dev_type
                    existing_co.is_mini_switch = is_mini
                    existing_co.passthrough_port = passthrough_p
                    existing_co.updated_at = now
                else:
                    session.add(
                        ClientOverride(
                            mac=clean_mac,
                            host=host,
                            device_type=dev_type,
                            is_mini_switch=is_mini,
                            passthrough_port=passthrough_p,
                            updated_at=now,
                        )
                    )

        return True

    def get_notes(self) -> Dict[str, str]:
        """Returns all port notes as a dictionary mapping 'ip:port' -> note."""
        notes = {}
        with get_db_session(self.db_url) as session:
            for pn in session.scalars(select(PortNote)):
                key = f"{pn.device_ip}:{pn.port}"
                notes[key] = pn.note or ""
                if not str(pn.port).startswith("Port "):
                    notes[f"{pn.device_ip}:Port {pn.port}"] = pn.note or ""
        return notes

    def save_notes(self, notes: Dict[str, str]):
        """Persists port notes to the database."""
        now = time.time()
        with get_db_session(self.db_url) as session:
            session.execute(delete(PortNote))
            seen = set()
            for key, note_text in notes.items():
                if ":" in str(key):
                    p_ip, p_num = str(key).split(":", 1)
                    p_num = p_num.replace("Port ", "").strip()
                    pair = (p_ip, p_num)
                    if p_num and str(note_text).strip() and pair not in seen:
                        seen.add(pair)
                        session.add(PortNote(device_ip=p_ip, port=p_num, note=str(note_text).strip(), updated_at=now))

    def _upsert_setting(self, session: Session, key: str, value: Any, now: float):
        """Helper to upsert a JSON setting record."""
        existing = session.scalar(select(ConfigSetting).where(ConfigSetting.key == key))
        val_str = json.dumps(value)
        if existing:
            existing.value_json = val_str
            existing.updated_at = now
        else:
            session.add(ConfigSetting(key=key, value_json=val_str, updated_at=now))
