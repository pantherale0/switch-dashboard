from datetime import datetime, timezone
import logging
from flask import Blueprint, render_template, jsonify, request, current_app

from switch_dashboard.config import load_config, save_config
from switch_dashboard.storage.repositories.scanner_repo import ScannerRepository

logger = logging.getLogger("switch_dashboard.web.scanner")

scanner_bp = Blueprint("scanner", __name__)


def find_client_by_ip(ip_address: str):
    cfg = load_config()
    db_clients = cfg.get("clients", {})
    mac_clean = ip_address.replace(":", "").replace("-", "").replace(" ", "").upper()
    if len(mac_clean) == 12 and all(ch in "0123456789ABCDEF" for ch in mac_clean):
        if mac_clean in db_clients:
            return mac_clean, db_clients[mac_clean]

    for mac_c, c in db_clients.items():
        if c.get("scanner_ip") == ip_address:
            return mac_c, c

    for mac_c, c in db_clients.items():
        if c.get("ip") == ip_address:
            return mac_c, c

    return None, None


@scanner_bp.route("/scanner")
def scanner_dashboard():
    cfg = load_config()
    settings = cfg.get("settings", {})
    col_widths = cfg.get("scanner_column_widths", settings.get("scanner_column_widths", {
        "del": 45,
        "ip_address": 120,
        "mac_address": 140,
        "vendor": 140,
        "hostname": 140,
        "known_host": 75,
        "status": 100,
        "ports": 120,
        "note": 180,
        "first_seen": 145,
        "last_seen_online": 145,
        "last_updated": 145,
    }))
    col_order = cfg.get("scanner_column_order", settings.get("scanner_column_order", [
        "del", "ip_address", "mac_address", "vendor", "hostname",
        "known_host", "status", "ports", "note", "first_seen",
        "last_seen_online", "last_updated",
    ]))
    col_visibility = cfg.get("scanner_column_visibility", settings.get("scanner_column_visibility", {
        "del": True,
        "ip_address": True,
        "mac_address": True,
        "vendor": True,
        "hostname": True,
        "known_host": True,
        "status": True,
        "ports": True,
        "note": True,
        "first_seen": True,
        "last_seen_online": True,
        "last_updated": True,
    }))
    version = getattr(current_app, "version", "2.0.0")
    return render_template(
        "scanner.html",
        title=cfg.get("title", "Network Switch Dashboard"),
        version=version,
        column_widths=col_widths,
        column_order=col_order,
        column_visibility=col_visibility,
    )


@scanner_bp.route("/scanner/history")
def scanner_history():
    cfg = load_config()
    version = getattr(current_app, "version", "2.0.0")
    return render_template(
        "scanner_history.html",
        title=cfg.get("title", "Network Switch Dashboard"),
        version=version,
    )


@scanner_bp.route("/api/scanner/hosts")
def api_scanner_hosts():
    cfg = load_config()
    db_clients = cfg.get("clients", {})
    hosts_list = []
    for mac_clean, c in db_clients.items():
        if not c.get("scanner_detected"):
            continue
        hosts_list.append({
            "ip_address": c.get("scanner_ip", ""),
            "mac_address": c.get("mac", ""),
            "vendor": c.get("vendor", ""),
            "hostname": c.get("host", ""),
            "ports": c.get("ports", ""),
            "note": c.get("note", ""),
            "status": c.get("scanner_status", "ONLINE" if c.get("status") == "online" else "OFFLINE"),
            "known_host": int(c.get("known_host", 0)),
            "first_seen": c.get("first_seen", ""),
            "last_seen_online": c.get("last_seen_online", ""),
            "last_updated": c.get("last_updated", ""),
        })
    return jsonify(hosts_list)


@scanner_bp.route("/api/scanner/hosts/<ip_address>/known", methods=["POST"])
def api_scanner_known(ip_address: str):
    data = request.get_json(force=True, silent=True) or {}
    new_state = int(data.get("known", 0))

    cfg = load_config()
    mac_clean, client = find_client_by_ip(ip_address)
    if client:
        client["known_host"] = new_state
        cfg["clients"][mac_clean] = client
        save_config(cfg)
        return jsonify({"success": True})
    return jsonify({"error": "Host not found"}), 404


@scanner_bp.route("/api/scanner/hosts/<ip_address>/update", methods=["POST"])
def api_scanner_update_host(ip_address: str):
    data = request.get_json(force=True, silent=True) or {}
    field = data.get("field")
    value = data.get("value", "")
    if field not in ["hostname", "note"]:
        return jsonify({"error": "Field not updatable"}), 400

    cfg = load_config()
    mac_clean, client = find_client_by_ip(ip_address)
    if client:
        if field == "hostname":
            client["host"] = value
        elif field == "note":
            client["note"] = value
        client["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cfg["clients"][mac_clean] = client
        save_config(cfg)
        return jsonify({"success": True})
    return jsonify({"error": "Host not found"}), 404


@scanner_bp.route("/api/scanner/hosts/<ip_address>", methods=["DELETE"])
def api_scanner_delete_host(ip_address: str):
    cfg = load_config()
    mac_clean, client = find_client_by_ip(ip_address)
    if client:
        if "port" in client:
            client["scanner_detected"] = False
            cfg["clients"][mac_clean] = client
        else:
            cfg["clients"].pop(mac_clean, None)
        save_config(cfg)
        return jsonify({"success": True})
    return jsonify({"error": "Host not found"}), 404


@scanner_bp.route("/api/scanner/history")
def api_scanner_history():
    cfg = load_config()
    db_clients = cfg.get("clients", {})
    scanner_repo = ScannerRepository()

    all_history = scanner_repo.get_all_history()
    db_history = {}
    for ev in all_history:
        ip = ev["ip_address"]
        if ip not in db_history:
            db_history[ip] = []
        db_history[ip].append(ev)

    grouped_history = {}
    for mac_clean, c in db_clients.items():
        if not c.get("scanner_detected"):
            continue
        ip = c.get("scanner_ip") or c.get("ip", "")
        if not ip:
            continue

        events = db_history.get(ip, [])
        formatted_events = []
        for e in events:
            ts = e.get("event_time", "")
            if ts:
                try:
                    if "T" not in ts:
                        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                        ts_str = dt.isoformat() + "Z"
                    else:
                        ts_str = ts
                except Exception:
                    ts_str = ts
            else:
                ts_str = ""
            formatted_events.append({
                "status": int(e.get("status", 0)),
                "event_time": ts_str,
            })
        grouped_history[ip] = {
            "hostname": c.get("host", ""),
            "events": formatted_events,
        }
    return jsonify(grouped_history)


@scanner_bp.route("/api/scanner/history/<ip_address>", methods=["DELETE"])
def api_scanner_delete_host_history(ip_address: str):
    cfg = load_config()
    mac_clean = ip_address.replace(":", "").replace("-", "").replace(" ", "").upper()
    if len(mac_clean) == 12 and all(ch in "0123456789ABCDEF" for ch in mac_clean):
        client = cfg.get("clients", {}).get(mac_clean)
        target_ip = client.get("scanner_ip") or client.get("ip", "") if client else ""
    else:
        target_ip = ip_address

    if target_ip:
        scanner_repo = ScannerRepository()
        scanner_repo.delete_host_history(target_ip)
    return jsonify({"success": True})


@scanner_bp.route("/api/scanner/history/all", methods=["DELETE"])
def api_scanner_clear_all_history():
    scanner_repo = ScannerRepository()
    scanner_repo.clear_all_history()
    return jsonify({"success": True})
