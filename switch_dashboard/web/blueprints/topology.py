import io
import csv
import logging
from flask import Blueprint, render_template, jsonify, request, current_app

from switch_dashboard.config import load_config, save_config
from switch_dashboard.services.topology_service import get_topology_service
from switch_dashboard.storage.repositories.device_repo import DeviceRepository

logger = logging.getLogger("switch_dashboard.web.topology")

topology_bp = Blueprint("topology", __name__)


@topology_bp.route("/map")
@topology_bp.route("/network-map")
def network_map():
    cfg = load_config()
    version = getattr(current_app, "version", "2.0.0")
    return render_template(
        "map.html",
        title=cfg.get("title", "Switch Dashboard"),
        map_positions=cfg.get("map_positions", {}),
        version=version,
    )


@topology_bp.route("/api/topology")
def api_topology():
    try:
        topo_service = get_topology_service()
        data = topo_service.build_topology()
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error computing network topology: {e}", exc_info=True)
        return jsonify({"nodes": [], "links": []})


@topology_bp.route("/api/clients/update_host", methods=["POST"])
def api_clients_update_host():
    data = request.get_json(force=True, silent=True) or {}
    mac = data.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
    host = data.get("host", "").strip()
    if not mac:
        return jsonify({"error": "Missing mac"}), 400

    cfg = load_config()
    db_clients = cfg.get("clients", {})

    if mac in db_clients:
        db_clients[mac]["host"] = host
    else:
        formatted_mac = ":".join(mac[i:i + 2] for i in range(0, len(mac), 2)).upper()
        db_clients[mac] = {
            "mac": formatted_mac,
            "host": host,
            "ip": "",
            "port": "",
            "vlan": "",
            "status": "offline",
            "last_seen": 0,
        }

    cfg["clients"] = db_clients
    save_config(cfg)

    try:
        DeviceRepository().update_discovered_client_meta(mac, host=host)
    except Exception as e:
        logger.warning(f"Could not update client host in database: {e}")

    return jsonify({"status": "ok"})


@topology_bp.route("/api/clients/update_type", methods=["POST"])
def api_clients_update_type():
    data = request.get_json(force=True, silent=True) or {}
    mac = data.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
    device_type = data.get("type", "").strip()
    if not mac:
        return jsonify({"error": "Missing mac"}), 400

    cfg = load_config()
    db_clients = cfg.get("clients", {})

    if mac in db_clients:
        db_clients[mac]["device_type"] = device_type
    else:
        formatted_mac = ":".join(mac[i:i + 2] for i in range(0, len(mac), 2)).upper()
        db_clients[mac] = {
            "mac": formatted_mac,
            "host": "",
            "device_type": device_type,
            "ip": "",
            "port": "",
            "vlan": "",
            "status": "offline",
            "last_seen": 0,
        }

    cfg["clients"] = db_clients
    save_config(cfg)

    try:
        DeviceRepository().update_discovered_client_meta(mac, device_type=device_type)
    except Exception as e:
        logger.warning(f"Could not update client type in database: {e}")

    return jsonify({"status": "ok"})


@topology_bp.route("/api/clients/update_passthrough", methods=["POST"])
def api_clients_update_passthrough():
    data = request.get_json(force=True, silent=True) or {}
    raw_mac = data.get("mac", "")
    if str(raw_mac).startswith("phone_"):
        raw_mac = str(raw_mac)[6:]
    mac = str(raw_mac).replace(":", "").replace("-", "").replace(" ", "").upper()
    is_mini_switch = bool(data.get("is_mini_switch", True))
    if not mac:
        return jsonify({"error": "Missing mac"}), 400

    formatted_mac = ":".join(mac[i:i + 2] for i in range(0, len(mac), 2)).upper() if len(mac) == 12 else mac

    cfg = load_config()
    db_clients = cfg.get("clients", {})

    target_key = None
    for k in db_clients:
        if k.replace(":", "").replace("-", "").upper() == mac:
            target_key = k
            break

    if target_key:
        db_clients[target_key]["is_mini_switch"] = is_mini_switch
        db_clients[target_key]["passthrough"] = is_mini_switch
        if is_mini_switch:
            db_clients[target_key]["device_type"] = "phone"
    else:
        db_clients[mac] = {
            "mac": formatted_mac,
            "host": "",
            "device_type": "phone" if is_mini_switch else "client",
            "is_mini_switch": is_mini_switch,
            "passthrough": is_mini_switch,
            "ip": "",
            "port": "",
            "vlan": "",
            "status": "online",
            "last_seen": 0,
        }

    cfg["clients"] = db_clients
    save_config(cfg)

    try:
        DeviceRepository().update_discovered_client_passthrough(mac, is_mini_switch)
    except Exception as e:
        logger.warning(f"Could not update client passthrough in database: {e}")

    return jsonify({"status": "ok", "is_mini_switch": is_mini_switch})


@topology_bp.route("/api/clients/delete", methods=["POST"])
def api_clients_delete():
    data = request.get_json(force=True, silent=True) or {}
    mac = data.get("mac", "").replace(":", "").replace("-", "").replace(" ", "").upper()
    if not mac:
        return jsonify({"error": "Missing mac"}), 400

    cfg = load_config()
    db_clients = cfg.get("clients", {})
    map_positions = cfg.get("map_positions", {})

    deleted = False
    if mac in db_clients:
        del db_clients[mac]
        deleted = True
    if mac in map_positions:
        del map_positions[mac]
        deleted = True

    if deleted:
        cfg["clients"] = db_clients
        cfg["map_positions"] = map_positions
        save_config(cfg)

    try:
        DeviceRepository().delete_discovered_client(mac)
    except Exception as e:
        logger.warning(f"Could not delete client from database: {e}")

    return jsonify({"status": "ok"})


@topology_bp.route("/api/clients/import_csv", methods=["POST"])
def api_clients_import_csv():
    if "file" not in request.files:
        return jsonify({"error": "No file part in the request"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected for uploading"}), 400
    if not file.filename.endswith(".csv"):
        return jsonify({"error": "Only CSV files are allowed"}), 400

    try:
        content = file.read().decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(content))
        imported_count = 0

        cfg = load_config()
        db_clients = cfg.get("clients", {})

        for row in reader:
            if not row:
                continue
            row = [cell.strip() for cell in row]
            if len(row) >= 2:
                c0_lower = row[0].lower()
                c1_lower = row[1].lower()
                if "host" in c0_lower or "name" in c0_lower or "mac" in c1_lower:
                    continue

                host_name = row[0]
                mac_raw = row[1]
                mac = mac_raw.replace(":", "").replace("-", "").replace(" ", "").upper()
                if len(mac) == 12 and all(c in "0123456789ABCDEF" for c in mac):
                    formatted_mac = ":".join(mac[i:i + 2] for i in range(0, len(mac), 2)).upper()
                    if mac in db_clients:
                        db_clients[mac]["host"] = host_name
                    else:
                        db_clients[mac] = {
                            "mac": formatted_mac,
                            "host": host_name,
                            "ip": "",
                            "port": "",
                            "vlan": "",
                            "status": "offline",
                            "last_seen": 0,
                        }
                    imported_count += 1

        if imported_count > 0:
            cfg["clients"] = db_clients
            save_config(cfg)

        return jsonify({"status": "ok", "imported": imported_count})
    except Exception as e:
        logger.error(f"Error importing CSV: {e}")
        return jsonify({"error": str(e)}), 500
