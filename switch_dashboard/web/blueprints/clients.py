import logging
from flask import Blueprint, jsonify, render_template, request
from switch_dashboard.services.clients import get_client_monitor_service
from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.repositories.device_repo import DeviceRepository

logger = logging.getLogger("switch_dashboard.web.clients")

clients_bp = Blueprint("clients", __name__)


@clients_bp.route("/clients")
def clients_view():
    """Renders the Fing-style Active Network & Client Monitoring Hub."""
    return render_template("clients.html")


@clients_bp.route("/api/clients/subnets", methods=["GET"])
def get_subnets():
    """Returns detected subnets with live online/discovered counts."""
    try:
        service = get_client_monitor_service()
        subnets = service.get_subnets_summary()
        return jsonify({"status": "ok", "subnets": subnets})
    except Exception as e:
        logger.error(f"Error fetching subnets: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@clients_bp.route("/api/clients/monitoring/summary", methods=["GET"])
def get_summary():
    """Returns hero summary metrics for the Fing header cards."""
    try:
        subnet = request.args.get("subnet")
        service = get_client_monitor_service()
        summary = service.get_monitoring_summary(subnet_filter=subnet)
        return jsonify({"status": "ok", "summary": summary})
    except Exception as e:
        logger.error(f"Error fetching monitoring summary: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@clients_bp.route("/api/clients/monitoring/list", methods=["GET"])
def get_clients_list():
    """Returns filtered client list for the Fing table."""
    try:
        subnet = request.args.get("subnet")
        category = request.args.get("category")
        status = request.args.get("status")
        search = request.args.get("search")

        service = get_client_monitor_service()
        clients = service.get_client_list(
            subnet_filter=subnet,
            category_filter=category,
            status_filter=status,
            search=search,
        )
        return jsonify({"status": "ok", "count": len(clients), "clients": clients})
    except Exception as e:
        logger.error(f"Error fetching client list: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@clients_bp.route("/api/clients/<mac>/details", methods=["GET"])
def get_client_details(mac):
    """Returns Fing deep-dive dossier (Network Setup, WiFi AP, Bandwidth,

    Timeline, IP History).
    """
    try:
        service = get_client_monitor_service()
        details = service.get_client_details(mac)
        if not details:
            return jsonify({"status": "error", "message": "Client not found"}), 404
        return jsonify({"status": "ok", "client": details})
    except Exception as e:
        logger.error(f"Error fetching client details for {mac}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@clients_bp.route("/api/clients/<mac>/edit", methods=["POST"])
def edit_client_meta(mac):
    """Updates custom nickname, device type, or IP for a client."""
    try:
        data = request.get_json() or {}
        custom_name = data.get("custom_name")
        hostname = data.get("hostname")
        device_type = data.get("device_type")
        ip = data.get("ip")

        repo = DeviceRepository(get_db())
        repo.update_discovered_client_meta(
            mac=mac,
            host=custom_name,
            hostname=hostname,
            ip=ip,
            device_type=device_type,
        )
        return jsonify({"status": "ok", "message": "Client updated successfully"})
    except Exception as e:
        logger.error(f"Error updating client {mac}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
