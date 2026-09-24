import os
import sys
import json
import shutil
import logging
import threading
from typing import Any, Dict, List, Optional

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
        except Exception:
            pass


def ensure_directories():
    """Ensure essential directories exist."""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(DEVICE_TEMPLATES_DIR, exist_ok=True)


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

            if not data.get("devices") and not data.get("switches"):
                if os.path.exists(CONFIG_PATH):
                    try:
                        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                            raw = json.load(f)
                        if isinstance(raw, dict) and (raw.get("devices") or raw.get("switches")):
                            repo.save_full_config(raw)
                            data = repo.load_full_config()
                    except Exception:
                        pass

            if not data.get("devices") and not data.get("switches"):
                data = get_default_config()

            _cached_config = data
            return data
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

            # Test safeguard: prevent any test execution from touching real PROJECT_ROOT config.json
            is_test_env = bool(
                os.environ.get("PYTEST_CURRENT_TEST")
                or os.environ.get("TESTING") == "1"
                or "pytest" in sys.modules
            )
            real_config_path = os.path.abspath(os.path.join(PROJECT_ROOT, "config.json"))

            # Optional human-readable export to config.json
            if not (is_test_env and os.path.abspath(CONFIG_PATH) == real_config_path):
                try:
                    if os.path.abspath(CONFIG_PATH) == real_config_path and os.path.exists(CONFIG_PATH):
                        os.makedirs(BACKUP_DIR, exist_ok=True)
                        backup_dest = os.path.join(BACKUP_DIR, "config.json.bak")
                        shutil.copy2(CONFIG_PATH, backup_dest)

                    temp_path = f"{CONFIG_PATH}.tmp"
                    with open(temp_path, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2)
                    os.replace(temp_path, CONFIG_PATH)
                except Exception as e:
                    logger.debug(f"Could not export config.json: {e}")

            _cached_config = cfg
            return True
        except Exception as e:
            logger.error(f"Error saving config: {e}", exc_info=True)
            return False


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
