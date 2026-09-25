import os
import time
import logging
import json
import threading
import yaml
import ipaddress
from flask import Blueprint, render_template, jsonify, request, redirect, url_for, current_app

from switch_dashboard.config import (
    load_config,
    save_config,
    DEVICE_TEMPLATES_DIR,
    PROJECT_TEMPLATES_DIR,
    DEVICE_TYPES_YAML_PATH,
    VENDORS_TXT_PATH,
    OUI36_TXT_PATH,
    OUI_TXT_PATH,
)
from switch_dashboard.services.vendor_service import get_vendor_service
from switch_dashboard.services.poller_service import get_poller_service
from switch_dashboard.services.scanner_service import get_scanner_service
from switch_dashboard.core.ports import normalize_port
from switch_dashboard.protocols.registry import ProtocolRegistry
from switch_dashboard.security.crypto import redact_secrets
from switch_dashboard.security.network import validate_management_target

logger = logging.getLogger("switch_dashboard.web.config")

config_bp = Blueprint("config_bp", __name__)


def _validate_scanner_settings(values):
    try:
        network = ipaddress.ip_network(
            values.get("scanner_network_range", "192.168.1.0/24"), strict=False
        )
        if network.num_addresses > 4096 or not network.is_private:
            return "Scanner network must be a private CIDR containing at most 4096 addresses"
        ports = set()
        for part in str(values.get("scanner_port_scan_range", "22,80,443,8080")).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = map(int, part.split("-", 1))
                if not 1 <= start <= end <= 65535 or end - start + 1 > 1024:
                    return "Each scanner port range must contain at most 1024 valid ports"
                ports.update(range(start, end + 1))
            else:
                port = int(part)
                if not 1 <= port <= 65535:
                    return "Scanner ports must be between 1 and 65535"
                ports.add(port)
        if len(ports) > 1024:
            return "At most 1024 ports may be scanned"
        if not 1 <= int(values.get("scanner_port_scan_threads", 20)) <= 128:
            return "Scanner port threads must be between 1 and 128"
        if not 1 <= int(values.get("scanner_host_scan_threads", 4)) <= 32:
            return "Scanner host threads must be between 1 and 32"
        if int(values.get("scanner_interval", 60)) < 30:
            return "Scanner interval must be at least 30 seconds"
        if int(values.get("scanner_port_scan_interval", 300)) < 60:
            return "Port scan interval must be at least 60 seconds"
        if not 100 <= int(values.get("scanner_port_scan_timeout_ms", 500)) <= 10000:
            return "Port scan timeout must be between 100 and 10000 milliseconds"
    except (TypeError, ValueError):
        return "Scanner settings contain an invalid number or CIDR"
    return None


def trigger_background_poll():
    try:
        poller = get_poller_service()
        threading.Thread(target=poller.poll_all_switches, daemon=True, name="ManualPollThread").start()
    except Exception as e:
        logger.debug(f"Poller reload notice: {e}")


@config_bp.route("/api/templates")
def list_templates():
    try:
        os.makedirs(DEVICE_TEMPLATES_DIR, exist_ok=True)
        files = set()
        for d in [DEVICE_TEMPLATES_DIR, PROJECT_TEMPLATES_DIR]:
            if os.path.exists(d):
                files.update(f for f in os.listdir(d) if f.endswith(".yaml"))
        return jsonify({"templates": sorted(list(files))})
    except Exception as e:
        logger.error(f"Failed to list YAML templates: {e}")
        return jsonify({"error": str(e)}), 500


@config_bp.route("/api/templates/<filename>", methods=["GET", "POST"])
def manage_template(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename or not filename.endswith(".yaml"):
        return jsonify({"error": "Invalid filename"}), 400

    filepath = os.path.join(DEVICE_TEMPLATES_DIR, filename)

    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        content = data.get("content", "")
        if not content.strip():
            return jsonify({"error": "Content cannot be empty"}), 400
        try:
            yaml.safe_load(content)
        except Exception as ye:
            return jsonify({"error": f"Invalid YAML Syntax: {ye}"}), 400

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"Updated YAML template: {filename}")
            return jsonify({"status": "ok"})
        except Exception as e:
            logger.error(f"Failed to save template {filename}: {e}")
            return jsonify({"error": str(e)}), 500

    # GET
    if not os.path.exists(filepath):
        # Fallback to project templates
        fallback = os.path.join(PROJECT_TEMPLATES_DIR, filename)
        if os.path.exists(fallback):
            filepath = fallback
        else:
            return jsonify({"error": "Template not found"}), 404
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return jsonify({"content": content})
    except Exception as e:
        logger.error(f"Failed to read template {filename}: {e}")
        return jsonify({"error": str(e)}), 500


@config_bp.route("/api/devices", methods=["GET"])
def api_get_devices():
    cfg = load_config()
    return jsonify({"devices": [redact_secrets(d) for d in cfg.get("devices", [])]})


@config_bp.route("/api/devices/upstream-candidates", methods=["GET"])
def api_get_upstream_candidates():
    cfg = load_config()
    candidates = []
    known_keys = set()

    for d in cfg.get("devices", []):
        key = d.get("id") or d.get("ip") or d.get("mac")
        if key and key not in known_keys:
            known_keys.add(key)
            is_phone = d.get("device_type") == "phone" or d.get("role") == "phone"
            candidates.append({
                "id": d.get("id"),
                "name": d.get("name"),
                "ip": d.get("ip", ""),
                "mac": d.get("mac", ""),
                "management_type": d.get("management_type", "managed"),
                "device_type": d.get("device_type", "switch"),
                "role": d.get("role", d.get("device_type", "switch")),
                "is_mini_switch": d.get("is_mini_switch", False) or is_phone,
                "is_phone": is_phone,
                "passthrough_port": d.get("passthrough_port", "PC"),
            })

    db_clients = cfg.get("clients", {})
    for cmac, cinfo in db_clients.items():
        if cinfo.get("device_type") == "phone":
            clean_m = str(cinfo.get("mac", cmac)).replace(":", "").replace("-", "").upper()
            norm_m = ":".join(clean_m[i:i + 2] for i in range(0, len(clean_m), 2)) if clean_m else cmac
            cip = cinfo.get("ip", "")
            pid = f"phone_{clean_m}"
            if pid not in known_keys and norm_m not in known_keys and (not cip or cip not in known_keys):
                known_keys.add(pid)
                candidates.append({
                    "id": pid,
                    "name": cinfo.get("host") or f"Phone ({cip or norm_m[-8:]})",
                    "ip": cip,
                    "mac": norm_m,
                    "management_type": "unmanaged",
                    "device_type": "phone",
                    "role": "phone",
                    "is_mini_switch": cinfo.get("is_mini_switch", True),
                    "is_phone": True,
                    "passthrough_port": cinfo.get("passthrough_port", "PC"),
                })

    return jsonify({"candidates": candidates})


@config_bp.route("/api/devices", methods=["POST"])
def api_add_device():
    data = request.get_json(force=True, silent=True) or {}
    if {"scrape_command", "status_command", "key_filename"}.intersection(data):
        return jsonify({"error": "Remote command and key path fields cannot be managed through the API"}), 400
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Device name is required"}), 400

    management_type = data.get("management_type", "managed")
    device_id = data.get("id") or f"dev_{int(time.time() * 1000)}"
    data["id"] = device_id
    data["management_type"] = management_type

    if "parent_port" in data and data["parent_port"]:
        data["parent_port"] = normalize_port(data["parent_port"])
    if "stats_port" in data and data["stats_port"]:
        data["stats_port"] = normalize_port(data["stats_port"])
    if "uplink_port" in data and data["uplink_port"]:
        data["uplink_port"] = normalize_port(data["uplink_port"])

    if data.get("role") in ["core_switch", "dist_switch", "access_switch"]:
        data["role"] = "switch"
    if data.get("device_type") in ["core_switch", "dist_switch", "access_switch"]:
        data["device_type"] = "switch"

    if management_type == "managed":
        try:
            validate_management_target(str(data.get("ip", "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        proto = data.get("protocol", "http_hc")
        is_valid, err, coerced = ProtocolRegistry.validate_config(proto, data)
        if not is_valid:
            return jsonify({"error": f"Invalid protocol configuration: {err}"}), 400
        data.update(coerced)

    cfg = load_config()
    devices = cfg.get("devices", [])
    devices = [d for d in devices if d.get("id") != device_id]
    devices.append(data)
    cfg["devices"] = devices

    if not save_config(cfg):
        return jsonify({"error": "Failed to save configuration"}), 500

    trigger_background_poll()

    return jsonify({"status": "ok", "device": redact_secrets(data)}), 201


@config_bp.route("/api/devices/<device_id>", methods=["PUT"])
def api_update_device(device_id: str):
    data = request.get_json(force=True, silent=True) or {}
    if {"scrape_command", "status_command", "key_filename"}.intersection(data):
        return jsonify({"error": "Remote command and key path fields cannot be managed through the API"}), 400
    cfg = load_config()
    devices = cfg.get("devices", [])

    idx = -1
    for i, d in enumerate(devices):
        if str(d.get("id")) == str(device_id) or str(d.get("ip")) == str(device_id):
            idx = i
            break

    if idx == -1:
        return jsonify({"error": f"Device '{device_id}' not found"}), 404

    current_dev = devices[idx]
    current_dev.update(data)
    current_dev["id"] = device_id

    if "parent_port" in current_dev and current_dev["parent_port"]:
        current_dev["parent_port"] = normalize_port(current_dev["parent_port"])
    if "stats_port" in current_dev and current_dev["stats_port"]:
        current_dev["stats_port"] = normalize_port(current_dev["stats_port"])
    if "uplink_port" in current_dev and current_dev["uplink_port"]:
        current_dev["uplink_port"] = normalize_port(current_dev["uplink_port"])

    if current_dev.get("role") in ["core_switch", "dist_switch", "access_switch"]:
        current_dev["role"] = "switch"
    if current_dev.get("device_type") in ["core_switch", "dist_switch", "access_switch"]:
        current_dev["device_type"] = "switch"

    if current_dev.get("management_type") == "managed":
        try:
            validate_management_target(str(current_dev.get("ip", "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        proto = current_dev.get("protocol", "http_hc")
        is_valid, err, coerced = ProtocolRegistry.validate_config(proto, current_dev)
        if not is_valid:
            return jsonify({"error": f"Invalid protocol configuration: {err}"}), 400
        current_dev.update(coerced)

    devices[idx] = current_dev
    cfg["devices"] = devices

    if not save_config(cfg):
        return jsonify({"error": "Failed to save configuration"}), 500

    trigger_background_poll()

    for field in data.get("clear_secrets", []):
        current_dev.pop(str(field), None)
    return jsonify({"status": "ok", "device": redact_secrets(current_dev)})


@config_bp.route("/api/devices/<device_id>", methods=["DELETE"])
def api_delete_device(device_id: str):
    cfg = load_config()
    devices = cfg.get("devices", [])

    filtered = [
        d for d in devices
        if str(d.get("id")) != str(device_id) and str(d.get("ip")) != str(device_id)
    ]
    if len(filtered) == len(devices):
        return jsonify({"error": f"Device '{device_id}' not found"}), 404

    cfg["devices"] = filtered
    if not save_config(cfg):
        return jsonify({"error": "Failed to save configuration"}), 500

    trigger_background_poll()

    return jsonify({"status": "ok", "message": f"Device '{device_id}' deleted"})


@config_bp.route("/config", methods=["GET", "POST"])
def config_page():
    if request.method == "POST":
        cfg = load_config()
        devices_json = request.form.get("devices_json")

        if devices_json:
            try:
                parsed_devices = json.loads(devices_json)
                if isinstance(parsed_devices, list):
                    for dev in parsed_devices:
                        if dev.get("management_type") == "managed":
                            validate_management_target(str(dev.get("ip", "")))
                        if "parent_port" in dev and dev["parent_port"]:
                            dev["parent_port"] = normalize_port(dev["parent_port"])
                        if "uplink_port" in dev and dev["uplink_port"]:
                            dev["uplink_port"] = normalize_port(dev["uplink_port"])
                    cfg["devices"] = parsed_devices
            except Exception as e:
                logger.error(f"Failed to parse devices_json: {e}")
        else:
            new_switches = []
            names = request.form.getlist("name[]")
            ips = request.form.getlist("ip[]")
            usernames = request.form.getlist("username[]")
            passwords = request.form.getlist("password[]")
            models = request.form.getlist("model[]")
            port_counts = request.form.getlist("port_count[]")
            keep = request.form.getlist("keep[]")
            enabled_vals = request.form.getlist("switch_enabled[]")

            parent_ips = request.form.getlist("parent_ip[]")
            parent_ports = request.form.getlist("parent_port[]")
            uplink_ports = request.form.getlist("uplink_port[]")

            for i in range(len(ips)):
                if i >= len(keep) or keep[i] != "1":
                    continue
                is_enabled = True
                if i < len(enabled_vals):
                    is_enabled = (enabled_vals[i] == "1")

                p_ip = parent_ips[i].strip() if i < len(parent_ips) else ""
                p_port = normalize_port(parent_ports[i]) if i < len(parent_ports) else ""
                up_port = normalize_port(uplink_ports[i]) if i < len(uplink_ports) else ""

                sw_dict = {
                    "name": names[i] if i < len(names) else f"Switch {i+1}",
                    "ip": ips[i].strip(),
                    "username": usernames[i].strip() if i < len(usernames) else "admin",
                    "password": passwords[i].strip() if i < len(passwords) else "admin",
                    "model": models[i].strip() if i < len(models) else "",
                    "port_count": int(port_counts[i]) if i < len(port_counts) else 9,
                    "enabled": is_enabled,
                }
                if p_ip:
                    sw_dict["parent_ip"] = p_ip
                if p_port:
                    sw_dict["parent_port"] = p_port
                if up_port:
                    sw_dict["uplink_port"] = up_port

                new_switches.append(sw_dict)

            infra_names = request.form.getlist("infra_name[]")
            infra_macs = request.form.getlist("infra_mac[]")
            infra_types = request.form.getlist("infra_type[]")
            infra_keeps = request.form.getlist("infra_keep[]")

            new_infra = []
            for i in range(len(infra_macs)):
                if i >= len(infra_keeps) or infra_keeps[i] != "1":
                    continue
                mac = infra_macs[i].strip().replace("-", ":").upper()
                if mac:
                    new_infra.append({
                        "name": infra_names[i].strip() if i < len(infra_names) else f"Device {i+1}",
                        "mac": mac,
                        "type": infra_types[i].strip() if i < len(infra_types) else "other",
                    })

            unmanaged_names = request.form.getlist("unmanaged_name[]")
            unmanaged_parent_ips = request.form.getlist("unmanaged_parent_ip[]")
            unmanaged_parent_ports = request.form.getlist("unmanaged_parent_port[]")
            unmanaged_keeps = request.form.getlist("unmanaged_keep[]")

            new_unmanaged = []
            for i in range(len(unmanaged_names)):
                if i >= len(unmanaged_keeps) or unmanaged_keeps[i] != "1":
                    continue
                name = unmanaged_names[i].strip()
                parent_ip = unmanaged_parent_ips[i].strip() if i < len(unmanaged_parent_ips) else ""
                parent_port = normalize_port(unmanaged_parent_ports[i]) if i < len(unmanaged_parent_ports) else ""
                if name and parent_ip and parent_port:
                    new_unmanaged.append({
                        "name": name,
                        "parent_ip": parent_ip,
                        "parent_port": parent_port,
                    })

            cfg["switches"] = new_switches
            cfg["infrastructure_devices"] = new_infra
            cfg["unmanaged_switches"] = new_unmanaged

        cfg["title"] = request.form.get("title", cfg.get("title", ""))
        cfg["refresh_interval"] = int(request.form.get("refresh_interval", 30))
        cfg["mac_refresh_multiplier"] = int(request.form.get("mac_refresh_multiplier", 5))
        cfg["ports_wrap_threshold"] = int(request.form.get("ports_wrap_threshold", 0))
        cfg["max_request_retries"] = int(request.form.get("max_request_retries", 5))
        new_columns = request.form.getlist("columns[]")
        if not new_columns:
            new_columns = ["port", "status", "speed", "packets", "bytes", "info", "notes"]
        cfg["enabled_columns"] = new_columns
        cfg["grid_columns"] = request.form.get("grid_columns", "auto")

        cfg["scanner_enabled"] = request.form.get("scanner_enabled") == "true"
        cfg["scanner_network_range"] = request.form.get("scanner_network_range", "192.168.1.0/24").strip()
        cfg["scanner_port_scan_enabled"] = request.form.get("scanner_port_scan_enabled") == "true"
        cfg["scanner_port_scan_range"] = request.form.get("scanner_port_scan_range", "22,80,443,8080").strip()
        cfg["scanner_interval"] = int(request.form.get("scanner_interval", 60))
        cfg["scanner_port_scan_interval"] = int(request.form.get("scanner_port_scan_interval", 300))
        cfg["scanner_purge_history_hours"] = int(request.form.get("scanner_purge_history_hours", 72))
        cfg["scanner_port_scan_threads"] = int(request.form.get("scanner_port_scan_threads", 20))
        cfg["scanner_host_scan_threads"] = int(request.form.get("scanner_host_scan_threads", 4))
        cfg["scanner_port_scan_timeout_ms"] = int(request.form.get("scanner_port_scan_timeout_ms", 500))
        scanner_error = _validate_scanner_settings(cfg)
        if scanner_error:
            return jsonify({"error": scanner_error}), 400
        cfg["telemetry_enabled"] = request.form.get("telemetry_enabled") == "true"

        ignored_macs_raw = request.form.get("ignored_macs", "")
        ignored_macs_list = [p.strip() for p in ignored_macs_raw.split(",") if p.strip()]
        if "settings" not in cfg:
            cfg["settings"] = {}
        cfg["settings"]["ignored_macs"] = ignored_macs_list

        save_config(cfg)

        trigger_background_poll()

        return redirect(url_for("dashboard.dashboard"))

    cfg = load_config()
    settings = cfg.get("settings", {})
    ignored_macs_list = settings.get("ignored_macs", [])
    ignored_macs_str = ", ".join(ignored_macs_list) if isinstance(ignored_macs_list, list) else str(ignored_macs_list)

    version = getattr(current_app, "version", "2.0.0")
    return render_template(
        "config.html",
        title=cfg.get("title", "Switch Dashboard"),
        devices=[redact_secrets(d) for d in cfg.get("devices", [])],
        switches=[redact_secrets(d) for d in cfg.get("switches", [])],
        infrastructure_devices=cfg.get("infrastructure_devices", []),
        unmanaged_switches=cfg.get("unmanaged_switches", []),
        refresh=cfg.get("refresh_interval", 30),
        mac_multiplier=cfg.get("mac_refresh_multiplier", 5),
        ports_wrap_threshold=cfg.get("ports_wrap_threshold", 0),
        max_request_retries=cfg.get("max_request_retries", 5),
        enabled_columns=cfg.get("enabled_columns", ["port", "status", "speed", "packets", "bytes", "info", "notes"]),
        grid_columns=cfg.get("grid_columns", "auto"),
        ignored_macs=ignored_macs_str,
        scanner_enabled=cfg.get("scanner_enabled", False),
        scanner_network_range=cfg.get("scanner_network_range", "192.168.1.0/24"),
        scanner_port_scan_enabled=cfg.get("scanner_port_scan_enabled", True),
        scanner_port_scan_range=cfg.get("scanner_port_scan_range", "22,80,443,8080"),
        scanner_interval=cfg.get("scanner_interval", 60),
        scanner_port_scan_interval=cfg.get("scanner_port_scan_interval", 300),
        scanner_purge_history_hours=cfg.get("scanner_purge_history_hours", 72),
        scanner_port_scan_threads=cfg.get("scanner_port_scan_threads", 20),
        scanner_host_scan_threads=cfg.get("scanner_host_scan_threads", 4),
        scanner_port_scan_timeout_ms=cfg.get("scanner_port_scan_timeout_ms", 500),
        version=version,
        telemetry_enabled=cfg.get("telemetry_enabled", True),
    )


@config_bp.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    cfg = load_config()
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        proposed = dict(cfg)
        proposed.update({k: v for k, v in data.items() if k.startswith("scanner_")})
        scanner_error = _validate_scanner_settings(proposed)
        if scanner_error:
            return jsonify({"error": scanner_error}), 400
        if "settings" not in cfg:
            cfg["settings"] = {}
        for k, v in data.items():
            if k.startswith("scanner_"):
                cfg[k] = v
            else:
                cfg["settings"][k] = v
        save_config(cfg)
        return jsonify({"status": "ok"})

    res = dict(cfg.get("settings", {}))
    for k in [
        "scanner_enabled", "scanner_network_range", "scanner_port_scan_enabled",
        "scanner_port_scan_range", "scanner_interval", "scanner_port_scan_interval",
        "scanner_purge_history_hours", "scanner_port_scan_threads",
        "scanner_host_scan_threads", "scanner_port_scan_timeout_ms",
    ]:
        if k in cfg:
            res[k] = cfg[k]

    return jsonify(res)


@config_bp.route("/api/config/settings", methods=["GET", "POST"])
def api_config_settings():
    cfg = load_config()
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        for key in ["column_widths", "column_order", "map_positions"]:
            if key in data:
                cfg[key] = data[key]
        save_config(cfg)
        return jsonify({"status": "ok"})

    return jsonify({
        "column_widths": cfg.get("column_widths", {}),
        "column_order": cfg.get("column_order", []),
        "map_positions": cfg.get("map_positions", {}),
    })


@config_bp.route("/api/device_types/raw", methods=["GET", "POST"])
def api_device_types_raw():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        content = data.get("content", "")
        if not content.strip():
            return jsonify({"error": "Content cannot be empty"}), 400
        try:
            parsed = yaml.safe_load(content)
            if not isinstance(parsed, dict):
                return jsonify({"error": "YAML must be a key-value dictionary."}), 400
            for k, v in parsed.items():
                if not isinstance(v, dict) or "label" not in v:
                    return jsonify({"error": f"Entry '{k}' must contain a 'label' key."}), 400
                if "icon" not in v and "path" not in v:
                    return jsonify({"error": f"Entry '{k}' must contain either 'icon' or 'path' key."}), 400
        except Exception as ye:
            return jsonify({"error": f"Invalid YAML Syntax: {ye}"}), 400

        try:
            with open(DEVICE_TYPES_YAML_PATH, "w", encoding="utf-8") as f:
                f.write(content)
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    content = ""
    if os.path.exists(DEVICE_TYPES_YAML_PATH):
        try:
            with open(DEVICE_TYPES_YAML_PATH, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"content": content})


@config_bp.route("/api/device_types", methods=["GET"])
def api_device_types():
    if os.path.exists(DEVICE_TYPES_YAML_PATH):
        try:
            with open(DEVICE_TYPES_YAML_PATH, "r", encoding="utf-8") as f:
                return jsonify(yaml.safe_load(f) or {})
        except Exception as e:
            logger.error(f"Failed to parse device_types.yaml: {e}")
    return jsonify({})


@config_bp.route("/api/vendors", methods=["GET", "POST"])
def api_vendors():
    vendor_service = get_vendor_service()
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        content = data.get("content", "")
        try:
            with open(VENDORS_TXT_PATH, "w", encoding="utf-8") as f:
                f.write(content)
            vendor_service.refresh_custom_vendors()
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    content = ""
    if os.path.exists(VENDORS_TXT_PATH):
        try:
            with open(VENDORS_TXT_PATH, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        content = (
            "# Custom MAC Vendor mappings (one per line)\n"
            "# Format: AA:BB:CC Vendor Name\n"
            "AA:BB:CC Custom Local Device\n"
            "00:11:22 Custom Router\n"
        )
        try:
            with open(VENDORS_TXT_PATH, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            pass
    return jsonify({"content": content})


@config_bp.route("/api/vendors/update_oui", methods=["POST"])
def update_oui_api():
    vendor_service = get_vendor_service()
    try:
        success_36 = vendor_service.download_single_oui_file("https://standards-oui.ieee.org/oui36/oui36.txt", OUI36_TXT_PATH)
        success_24 = vendor_service.download_single_oui_file("https://standards-oui.ieee.org/oui/oui.txt", OUI_TXT_PATH)
        if success_36 and success_24:
            return jsonify({"status": "ok", "message": "IEEE OUI databases successfully updated."})
        else:
            return jsonify({"status": "error", "message": "Failed to download one or both OUI files. Check server logs."}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
