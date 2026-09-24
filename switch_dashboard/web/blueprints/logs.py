import os
import logging
from flask import Blueprint, render_template, jsonify, request, send_from_directory, make_response, current_app

from switch_dashboard.config import LOG_FILE_PATH, load_config, save_config
from switch_dashboard.services.log_service import (
    read_recent_logs,
    set_log_level,
    clear_logs,
    get_current_log_level,
)

logger = logging.getLogger("switch_dashboard.web.logs")

logs_bp = Blueprint("logs", __name__)


@logs_bp.route("/logs")
def logs_page():
    cfg = load_config()
    version = getattr(current_app, "version", "2.0.0")
    log_level = get_current_log_level()

    rendered = render_template(
        "logs.html",
        title=cfg.get("title", "Switch Dashboard"),
        version=version,
        current_log_level=log_level,
    )
    resp = make_response(rendered)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@logs_bp.route("/api/logs")
def api_logs():
    lines = read_recent_logs(150)
    resp = jsonify(lines)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@logs_bp.route("/api/logs/level", methods=["POST"])
def api_logs_level():
    data = request.get_json(force=True, silent=True) or {}
    level_name = data.get("level", "INFO").upper()
    valid_levels = ["DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "NONE"]
    if level_name not in valid_levels:
        return jsonify({"error": "Invalid log level"}), 400

    try:
        cfg = load_config()
        if "settings" not in cfg:
            cfg["settings"] = {}
        cfg["settings"]["log_level"] = level_name
        save_config(cfg)

        set_log_level(level_name)
        logger.warning(f"Log level dynamically changed to {level_name} by user request.")
        return jsonify({"status": "ok", "level": level_name})
    except Exception as e:
        logger.error(f"Failed to update log level: {e}")
        return jsonify({"error": str(e)}), 500


@logs_bp.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    success = clear_logs()
    if success:
        return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to clear logs"}), 500


@logs_bp.route("/api/logs/download")
def api_logs_download():
    if not os.path.exists(LOG_FILE_PATH):
        try:
            with open(LOG_FILE_PATH, "w", encoding="utf-8") as f:
                pass
        except Exception:
            return jsonify({"error": "Log file not found"}), 404

    dir_name = os.path.dirname(LOG_FILE_PATH)
    file_name = os.path.basename(LOG_FILE_PATH)
    return send_from_directory(dir_name, file_name, as_attachment=True)
