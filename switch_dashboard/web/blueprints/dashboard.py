import os
import time
import logging
from flask import Blueprint, render_template, jsonify, send_from_directory, current_app

from switch_dashboard.config import (
    load_config,
    get_config,
    DEVICE_TEMPLATES_DIR,
    PROJECT_TEMPLATES_DIR,
    PROJECT_ROOT,
)
from switch_dashboard.services.poller_service import get_poller_service
from switch_dashboard.services.vendor_service import get_vendor_service
from switch_dashboard.services.topology_service import is_ignored_mac
from switch_dashboard.protocols.registry import ProtocolRegistry
from switch_dashboard.protocols.base import Capability
from switch_dashboard.core.ports import port_key

logger = logging.getLogger("switch_dashboard.web.dashboard")

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def dashboard():
    cfg = load_config()
    version = getattr(current_app, "version", "2.0.0")
    return render_template(
        "index.html",
        title=cfg.get("title", "Switch Dashboard"),
        refresh=cfg.get("settings", {}).get("refresh_interval", 30),
        enabled_columns=cfg.get("enabled_columns", ["port", "status", "speed", "packets", "bytes", "info", "notes"]),
        grid_columns=cfg.get("grid_columns", "auto"),
        ports_wrap_threshold=cfg.get("ports_wrap_threshold", 0),
        column_widths=cfg.get("column_widths", {}),
        column_order=cfg.get("column_order", []),
        version=version,
    )


@dashboard_bp.route("/api/switches")
def api_switches():
    cfg = load_config()
    switch_configs = cfg.get("switches", [])
    notes = cfg.get("notes", {})
    db_clients = cfg.get("clients", {})

    vendor_service = get_vendor_service()
    poller = get_poller_service()

    active_ips = [
        sw["ip"]
        for sw in switch_configs
        if sw.get("enabled", True) and sw.get("model", "").lower() != "internet"
    ]
    cached = poller.get_cached_data()
    data = [dict(cached[ip]) for ip in active_ips if ip in cached]

    from switch_dashboard.storage.repositories.device_repo import DeviceRepository
    from switch_dashboard.services.host_discovery_service import get_host_discovery_service

    disc_repo = DeviceRepository().get_all_discovered_clients()
    host_disc_service = get_host_discovery_service()
    switches_dict = {sw["ip"]: sw for sw in data if "ip" in sw}
    all_infra_ips = {sw.get("ip") for sw in switch_configs if sw.get("ip")}
    discovered_host_map = host_disc_service.resolve_all_clients(switches_dict, infra_ips=all_infra_ips)

    sw_configs_map = {sw["ip"]: sw for sw in switch_configs}
    for sw in data:
        ip = sw.get("ip", "")
        sw_conf = sw_configs_map.get(ip, {})
        proto_name = sw_conf.get("protocol") or sw.get("protocol") or sw_conf.get("model") or "http_hc"
        sw["protocol"] = proto_name
        proto_cls = ProtocolRegistry.get_protocol_class(proto_name)
        sw["can_backup"] = Capability.BACKUP in getattr(proto_cls, "capabilities", set()) if proto_cls else False
        if "device_type" not in sw or not sw["device_type"]:
            sw["device_type"] = sw_conf.get("device_type") or sw_conf.get("role") or sw.get("role") or "switch"
        mac_tbl = sw.get("mac_table")
        if not mac_tbl:
            mac_tbl = DeviceRepository().get_mac_table(sw.get("ip", ""))
        sw["mac_table"] = [
            entry for entry in (mac_tbl or [])
            if not is_ignored_mac(entry.get("mac"))
        ]
        for entry in sw.get("mac_table", []):
            entry["vendor"] = vendor_service.lookup_vendor(entry.get("mac"))
            norm_mac = entry.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
            host_info = discovered_host_map.get(norm_mac, {})
            disc_entry = disc_repo.get(norm_mac, {})

            if norm_mac in db_clients:
                entry["host"] = db_clients[norm_mac].get("host", "")
            else:
                entry["host"] = disc_entry.get("custom_name", "")

            disc_h = host_info.get("hostname") or disc_entry.get("hostname") or entry.get("hostname") or ""
            if disc_h.startswith("Client "):
                disc_h = ""
            entry["hostname"] = disc_h

            entry["ip"] = (
                entry.get("ip")
                or host_info.get("ip")
                or disc_entry.get("ip")
                or ""
            )
        for p in sw.get("ports", []):
            p_raw = p.get("port")
            custom_note = (
                notes.get(port_key(sw["ip"], p_raw)) or
                notes.get(f"{sw['ip']}:{p_raw}") or
                notes.get(f"{sw['ip']}:Port {p_raw}") or
                ""
            )
            if custom_note:
                p["note"] = custom_note
            else:
                p["note"] = p.get("vm_name") or ""
    return jsonify(data)


@dashboard_bp.route("/api/switches/<ip>/refresh_mac", methods=["POST"])
@dashboard_bp.route("/refresh_mac/<ip>", methods=["POST"])
def refresh_mac(ip: str):
    cfg = get_config()
    sw = next((s for s in cfg.get("switches", []) if s.get("ip") == ip), None)
    if not sw:
        return jsonify({"error": "Switch not found"}), 404

    if sw.get("model", "").lower() == "internet":
        return jsonify({"status": "ok", "count": 0, "mac_table": []})

    try:
        protocol = ProtocolRegistry.create(sw)
        mac_table = protocol.scrape_mac_table()
        mac_table = [entry for entry in mac_table if not is_ignored_mac(entry.get("mac"))]

        vendor_service = get_vendor_service()
        for entry in mac_table:
            entry["vendor"] = vendor_service.lookup_vendor(entry.get("mac"))

        poller = get_poller_service()
        with poller._cache_lock:
            if ip in poller._cached_data:
                poller._cached_data[ip]["mac_table"] = mac_table
                poller._cached_data[ip]["mac_timestamp"] = time.time()

        return jsonify({"status": "ok", "count": len(mac_table), "mac_table": mac_table})
    except Exception as e:
        logger.error(f"Manual MAC scrape failed for {ip}: {e}")
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/api/switches/<ip>/transceiver")
@dashboard_bp.route("/transceiver/<ip>")
def get_transceiver(ip: str):
    cfg = get_config()
    sw = next((s for s in cfg.get("switches", []) if s.get("ip") == ip), None)
    if not sw:
        return jsonify({"error": "Switch not found"}), 404

    if sw.get("model", "").lower() == "internet":
        return jsonify({"error": "No transceiver data available for virtual Internet node"}), 404

    try:
        protocol = ProtocolRegistry.create(sw)
        transceiver_info = protocol.scrape_transceiver()
        if not transceiver_info:
            return jsonify({"error": "No transceiver data available or SFP module not inserted"}), 404
        return jsonify(transceiver_info)
    except Exception as e:
        logger.error(f"Transceiver scrape failed for {ip}: {e}")
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/api/switches/<ip>/image")
def get_switch_image(ip: str):
    cfg = get_config()
    sw = next((s for s in cfg.get("switches", []) if s.get("ip") == ip), None)
    if sw:
        model = sw.get("model", "")
        if model:
            for ext in ["png", "jpg", "jpeg"]:
                img_name = f"{model}.{ext}"
                for d in [DEVICE_TEMPLATES_DIR, PROJECT_TEMPLATES_DIR]:
                    if os.path.exists(os.path.join(d, img_name)):
                        return send_from_directory(d, img_name)
    static_dir = os.path.join(PROJECT_ROOT, "static")
    return send_from_directory(static_dir, "switch_icon.png")
