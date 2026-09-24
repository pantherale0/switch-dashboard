import logging
from flask import Blueprint, jsonify, request

from switch_dashboard.services.poller_service import get_poller_service
from switch_dashboard.storage.repositories.metric_repo import get_metric_repo
from switch_dashboard.storage.repositories.config_repo import ConfigRepository
from switch_dashboard.core.ports import normalize_port, port_key

logger = logging.getLogger("switch_dashboard.web.metrics")

metrics_bp = Blueprint("metrics", __name__)


@metrics_bp.route("/api/speeds")
def api_speeds():
    poller = get_poller_service()
    return jsonify(poller.get_cached_speeds())


@metrics_bp.route("/api/history")
def api_history():
    ip = request.args.get("ip")
    port_param = request.args.get("port")
    range_type = request.args.get("range", "live")  # live, 1h, 24h

    if not ip or not port_param:
        return jsonify({"error": "Missing ip or port"}), 400

    metric_repo = get_metric_repo()
    result = metric_repo.get_history_range(ip, port_param, range_type)
    return jsonify(result)


@metrics_bp.route("/api/reset", methods=["POST"])
def api_reset():
    metric_repo = get_metric_repo()
    metric_repo.reset_all_counters()
    logger.info("Reset all counters and history via API.")
    return jsonify({"status": "ok"})


@metrics_bp.route("/api/notes", methods=["POST"])
def api_notes():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key")
    note = data.get("note", "")
    if not key:
        return jsonify({"error": "missing key"}), 400

    if ":" in str(key):
        parts = str(key).split(":", 1)
        canonical_key = port_key(parts[0], parts[1])
    else:
        canonical_key = str(key)

    config_repo = ConfigRepository()
    notes = config_repo.get_notes()
    if str(note).strip():
        notes[canonical_key] = str(note).strip()
    else:
        notes.pop(canonical_key, None)
        notes.pop(str(key), None)
    config_repo.save_notes(notes)
    return jsonify({"status": "ok"})

