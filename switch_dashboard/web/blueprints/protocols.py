from flask import Blueprint, jsonify, request
from switch_dashboard.protocols.registry import ProtocolRegistry
from switch_dashboard.services.poller_service import get_poller_service
from switch_dashboard.core.ports import normalize_port, format_port_display
from switch_dashboard.config import get_config
from switch_dashboard.security.network import validate_management_target

protocols_bp = Blueprint("protocols", __name__)


@protocols_bp.route("/api/protocols")
def api_protocols():
    """Returns list of registered protocol drivers and their capabilities."""
    return jsonify(ProtocolRegistry.list_protocols())


@protocols_bp.route("/api/protocols/<name>/validate", methods=["POST"])
def validate_protocol_config(name: str):
    """Validates parameters against a protocol driver's Voluptuous schema."""
    data = request.get_json(force=True, silent=True) or {}
    is_valid, error, coerced = ProtocolRegistry.validate_config(name, data)
    if not is_valid:
        return jsonify({"valid": False, "error": error}), 400
    return jsonify({"valid": True, "config": coerced})


@protocols_bp.route("/api/devices/test-connection", methods=["POST"])
def test_device_connection():
    """Tests device reachability and credentials with the selected driver."""
    data = request.get_json(force=True, silent=True) or {}
    ip = data.get("ip", "").strip()
    proto_name = data.get("protocol", "http_hc").strip()

    if not ip:
        return jsonify({"success": False, "message": "IP address is required"}), 400
    try:
        validate_management_target(ip)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    forbidden = {"scrape_command", "status_command", "key_filename", "bridge"}
    if forbidden.intersection(data):
        return jsonify({"success": False, "message": "Connection test contains restricted fields"}), 400

    # Validate against Voluptuous schema if defined
    is_valid, error, coerced = ProtocolRegistry.validate_config(proto_name, data)
    if not is_valid:
        return jsonify({"success": False, "message": f"Invalid configuration: {error}"}), 400

    try:
        driver = ProtocolRegistry.create(coerced)
        ok, msg = driver.test_connection()
        return jsonify({"success": ok, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "message": f"Connection failed: {str(e)}"}), 200


@protocols_bp.route("/api/devices/detect-upstream")
def detect_upstream():
    """Checks existing switch MAC forwarding tables to detect where a target IP/MAC is attached."""
    target_ip = request.args.get("ip", "").strip()
    target_mac = request.args.get("mac", "").replace(":", "").replace("-", "").upper()

    poller = get_poller_service()
    cached = poller.get_cached_data()
    cfg = get_config()
    switches = cfg.get("switches", [])
    sw_names = {s.get("ip"): s.get("name", s.get("ip")) for s in switches}

    # Find MAC if target_ip is given but not target_mac
    if target_ip and not target_mac:
        for sw_ip, sw_info in cached.items():
            for m in sw_info.get("mac_table", []):
                if m.get("ip") == target_ip and m.get("mac"):
                    target_mac = m.get("mac").replace(":", "").replace("-", "").upper()
                    break

    if not target_mac:
        return jsonify({"found": False, "message": "MAC not yet observed in network tables"})

    # Search for switch seeing this MAC on an access port
    candidates = []
    for sw_ip, sw_info in cached.items():
        if sw_ip == target_ip:
            continue
        for m in sw_info.get("mac_table", []):
            clean_m = (m.get("mac") or "").replace(":", "").replace("-", "").upper()
            if clean_m == target_mac:
                raw_port = m.get("port")
                norm_port = normalize_port(raw_port)
                candidates.append({
                    "upstream_ip": sw_ip,
                    "upstream_name": sw_names.get(sw_ip, sw_ip),
                    "upstream_port": norm_port,
                    "upstream_port_display": format_port_display(norm_port),
                })

    if candidates:
        best = candidates[0]
        return jsonify({"found": True, **best})

    return jsonify({"found": False, "message": "Device not yet observed on any switch port"})
