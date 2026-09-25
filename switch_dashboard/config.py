import os
import sys
import json
import shutil
import logging
import threading
from typing import Any, Dict, List, Optional

from switch_dashboard.security.crypto import CryptoConfigurationError, SECRET_FIELDS, encrypt_bytes, redact_secrets, redact_tree

logger = logging.getLogger("switch_dashboard.config")

# Project base directory (where app.py / templates / static reside)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR", PROJECT_ROOT)

# Paths
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DEVICE_TYPES_YAML_PATH = os.path.join(DATA_DIR, "device_types.yaml")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
NOTES_PATH = os.path.join(DATA_DIR, "notes.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")
LOG_FILE_PATH = os.path.join(LOG_DIR, "dashboard.log")
DATABASE_PATH = os.path.join(DATA_DIR, "dashboard.db")
LEGACY_SCANNER_DB_PATH = os.path.join(DATA_DIR, "network_scanner.db")
COUNTERS_PATH = os.path.join(DATA_DIR, "counters.json")
HOURLY_PATH = os.path.join(DATA_DIR, "history_hourly.json")
DAILY_PATH = os.path.join(DATA_DIR, "history_daily.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
DEVICE_TEMPLATES_DIR = os.path.join(DATA_DIR, "device-templates")
PROJECT_TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "device-templates")
VENDORS_TXT_PATH = os.path.join(DATA_DIR, "mac_vendors.txt")
OUI36_TXT_PATH = os.path.join(DATA_DIR, "oui36.txt")
OUI_TXT_PATH = os.path.join(DATA_DIR, "oui.txt")

_config_lock = threading.RLock()
_cached_config: Optional[Dict[str, Any]] = None


def _infer_protocol(device: Dict[str, Any]) -> str:
    if device.get("protocol"):
        return str(device["protocol"])
    model = str(device.get("model", "")).lower()
    if model in ("openvswitch", "ovs"):
        return "ovs"
    if model == "fritzbox":
        return "fritzbox"
    if model in ("snmp", "generic_snmp"):
        return "snmp"
    if model in ("ssh", "generic_ssh"):
        return "ssh"
    if model.startswith(("unifi", "usw", "uap", "usg", "udm", "ucg", "uxg")):
        return "unifi"
    if model in ("proxmox", "proxmox_ve", "pve"):
        return "proxmox"
    return "http_hc"


def set_data_dir(new_dir: str):
    """Dynamically switch the active data directory and update all path constants."""
    global DATA_DIR, CONFIG_PATH, DEVICE_TYPES_YAML_PATH, SETTINGS_PATH, NOTES_PATH
    global LOG_DIR, LOG_FILE_PATH, DATABASE_PATH, LEGACY_SCANNER_DB_PATH, COUNTERS_PATH
    global HOURLY_PATH, DAILY_PATH, BACKUP_DIR, DEVICE_TEMPLATES_DIR, VENDORS_TXT_PATH
    global OUI36_TXT_PATH, OUI_TXT_PATH, _cached_config

    with _config_lock:
        os.environ["DASHBOARD_DATA_DIR"] = new_dir
        DATA_DIR = os.path.abspath(new_dir)
        CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
        DEVICE_TYPES_YAML_PATH = os.path.join(DATA_DIR, "device_types.yaml")
        SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
        NOTES_PATH = os.path.join(DATA_DIR, "notes.json")
        LOG_DIR = os.path.join(DATA_DIR, "logs")
        LOG_FILE_PATH = os.path.join(LOG_DIR, "dashboard.log")
        DATABASE_PATH = os.path.join(DATA_DIR, "dashboard.db")
        LEGACY_SCANNER_DB_PATH = os.path.join(DATA_DIR, "network_scanner.db")
        COUNTERS_PATH = os.path.join(DATA_DIR, "counters.json")
        HOURLY_PATH = os.path.join(DATA_DIR, "history_hourly.json")
        DAILY_PATH = os.path.join(DATA_DIR, "history_daily.json")
        BACKUP_DIR = os.path.join(DATA_DIR, "backup")
        DEVICE_TEMPLATES_DIR = os.path.join(DATA_DIR, "device-templates")
        VENDORS_TXT_PATH = os.path.join(DATA_DIR, "mac_vendors.txt")
        OUI36_TXT_PATH = os.path.join(DATA_DIR, "oui36.txt")
        OUI_TXT_PATH = os.path.join(DATA_DIR, "oui.txt")
        _cached_config = None
        try:
            from switch_dashboard.storage.engine import reset_engine
            reset_engine()
            from switch_dashboard.storage.database import reset_database
            reset_database()
        except Exception:
            pass


def ensure_directories():
    """Ensure essential directories exist."""
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    os.makedirs(DEVICE_TEMPLATES_DIR, mode=0o700, exist_ok=True)
    for path in (DATA_DIR, LOG_DIR, BACKUP_DIR, DEVICE_TEMPLATES_DIR):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def get_default_config() -> Dict[str, Any]:
    return {
        "devices": [],
        "switches": [],
        "access_points": [],
        "unmanaged_switches": [],
        "infrastructure_devices": [],
        "settings": {
            "refresh_interval": 30,
            "mac_refresh_multiplier": 5,
            "max_request_retries": 5,
            "log_level": "INFO",
            "history_retention_days": 7,
            "trends_retention_days": 90,
            "scanner_enabled": False,
            "scanner_subnet": "192.168.1.0/24",
            "scanner_interval": 300,
            "port_scan_enabled": False,
            "port_scan_ports": "22,80,443",
            "port_scan_timeout_ms": 1000,
            "port_scan_threads": 10,
        },
        "notes": {},
    }


def load_config() -> Dict[str, Any]:
    """Thread-safe configuration loader backed by the database ConfigRepository with in-memory caching."""
    global _cached_config
    with _config_lock:
        if _cached_config is not None:
            return _cached_config

        ensure_directories()
        try:
            from switch_dashboard.storage.repositories.config_repo import ConfigRepository

            repo = ConfigRepository()
            repo.import_from_json_if_empty(CONFIG_PATH)
            data = repo.load_full_config()
            _sanitize_legacy_config_files(data)

            if not data.get("devices") and not data.get("switches"):
                if os.path.exists(CONFIG_PATH):
                    try:
                        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                            raw = json.load(f)
                        if isinstance(raw, dict) and (raw.get("devices") or raw.get("switches")):
                            repo.save_full_config(raw)
                            data = repo.load_full_config()
                    except CryptoConfigurationError:
                        raise
                    except Exception:
                        pass

            if not data.get("devices") and not data.get("switches"):
                defaults = get_default_config()
                defaults.update(data)
                defaults["settings"] = {
                    **get_default_config()["settings"],
                    **data.get("settings", {}),
                }
                data = defaults

            _cached_config = data
            return data
        except CryptoConfigurationError:
            raise
        except Exception as e:
            logger.error(f"Error loading configuration from database: {e}", exc_info=True)
            if _cached_config is not None:
                return _cached_config
            return get_default_config()


def save_config(cfg: Dict[str, Any]) -> bool:
    """Thread-safe configuration saver backed by the database ConfigRepository."""
    global _cached_config
    with _config_lock:
        ensure_directories()
        try:
            # Bidirectional projection: if "devices" is provided, project to legacy arrays
            if "devices" in cfg and isinstance(cfg["devices"], list):
                switches = []
                unmanaged = []
                infra = []
                for d in cfg["devices"]:
                    if d.get("role") in ["core_switch", "dist_switch", "access_switch"]:
                        d["role"] = "switch"
                    if d.get("device_type") in ["core_switch", "dist_switch", "access_switch"]:
                        d["device_type"] = "switch"
                    if d.get("management_type") == "managed":
                        switches.append(dict(d))
                    elif d.get("device_type") in ["unmanaged_switch", "phone", "ont"] or d.get("role") in ["unmanaged_switch", "phone", "ont"] or not d.get("mac"):
                        unmanaged.append(dict(d))
                    else:
                        infra.append(dict(d))
                cfg["switches"] = switches
                cfg["unmanaged_switches"] = unmanaged
                cfg["infrastructure_devices"] = infra
            elif "switches" in cfg or "unmanaged_switches" in cfg:
                # Update devices if legacy arrays were modified
                unified = []
                for idx, sw in enumerate(cfg.get("switches", [])):
                    dev = dict(sw)
                    dev.setdefault("id", f"dev_{sw.get('ip', idx)}")
                    dev.setdefault("management_type", "managed")
                    role = sw.get("role", "switch")
                    if role in ["core_switch", "dist_switch", "access_switch"]:
                        role = "switch"
                    dev.setdefault("device_type", role)
                    dev.setdefault("role", role)
                    unified.append(dev)
                for idx, us in enumerate(cfg.get("unmanaged_switches", [])):
                    dev = dict(us)
                    dev.setdefault("id", f"unmanaged_{us.get('parent_ip', '')}_{us.get('parent_port', idx)}")
                    dev.setdefault("management_type", "unmanaged")
                    dev.setdefault("device_type", us.get("device_type", "unmanaged_switch"))
                    unified.append(dev)
                for idx, item in enumerate(cfg.get("infrastructure_devices", [])):
                    dev = dict(item)
                    dev.setdefault("id", f"infra_{item.get('mac', idx)}")
                    dev.setdefault("management_type", "unmanaged")
                    dev.setdefault("device_type", item.get("type", "other"))
                    unified.append(dev)
                cfg["devices"] = unified

            # Persist to Database via ConfigRepository
            try:
                from switch_dashboard.storage.repositories.config_repo import ConfigRepository

                repo = ConfigRepository()
                repo.save_full_config(cfg)
            except Exception as e:
                logger.error(f"Error persisting configuration to database: {e}", exc_info=True)
                contains_secrets = any(
                    any(device.get(field) not in (None, "") for field in SECRET_FIELDS)
                    for device in cfg.get("devices", [])
                )
                if contains_secrets:
                    return False
                persistence_failed = True
            else:
                persistence_failed = False

            # Test safeguard: prevent any test execution from touching real PROJECT_ROOT config.json
            is_test_env = bool(
                os.environ.get("PYTEST_CURRENT_TEST")
                or os.environ.get("TESTING") == "1"
                or "pytest" in sys.modules
            )
            real_config_path = os.path.abspath(os.path.join(PROJECT_ROOT, "config.json"))

            # Optional human-readable export. Credentials are never exported.
            if not (is_test_env and os.path.abspath(CONFIG_PATH) == real_config_path):
                try:
                    if os.path.abspath(CONFIG_PATH) == real_config_path and os.path.exists(CONFIG_PATH):
                        os.makedirs(BACKUP_DIR, exist_ok=True)
                        backup_dest = os.path.join(BACKUP_DIR, "config.json.bak")
                        shutil.copy2(CONFIG_PATH, backup_dest)

                    temp_path = f"{CONFIG_PATH}.tmp"
                    export_cfg = redact_tree(cfg)
                    with open(temp_path, "w", encoding="utf-8") as f:
                        json.dump(export_cfg, f, indent=2)
                    os.chmod(temp_path, 0o600)
                    os.replace(temp_path, CONFIG_PATH)
                except Exception as e:
                    logger.debug(f"Could not export config.json: {e}")

            _cached_config = None
            return not persistence_failed
        except Exception as e:
            logger.error(f"Error saving config: {e}", exc_info=True)
            return False


def _sanitize_legacy_config_files(cfg: Dict[str, Any]) -> None:
    """Remove plaintext credentials from legacy JSON and encrypt old copies."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                exported = json.load(f)
            if isinstance(exported, dict):
                exported = redact_tree(exported)
                temp_path = f"{CONFIG_PATH}.tmp"
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(exported, f, indent=2)
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, CONFIG_PATH)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(f"Could not sanitize legacy config export: {exc}")

    for legacy_path in (f"{CONFIG_PATH}.bak", os.path.join(BACKUP_DIR, "config.json.bak")):
        if not os.path.isfile(legacy_path):
            continue
        encrypted_path = f"{legacy_path}.enc"
        try:
            with open(legacy_path, "rb") as f:
                encrypted = encrypt_bytes(f.read(), f"legacy-config:{os.path.basename(encrypted_path)}")
            fd = os.open(f"{encrypted_path}.tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(encrypted)
            os.replace(f"{encrypted_path}.tmp", encrypted_path)
            os.remove(legacy_path)
        except Exception as exc:
            logger.warning(f"Could not encrypt legacy config backup {legacy_path}: {exc}")


def get_config() -> Dict[str, Any]:
    """Retrieves current cached or loaded configuration."""
    global _cached_config
    if _cached_config is None:
        return load_config()
    return _cached_config


def get_setting(key: str, default: Any = None) -> Any:
    cfg = get_config()
    settings = cfg.get("settings", {})
    return settings.get(key, default)
