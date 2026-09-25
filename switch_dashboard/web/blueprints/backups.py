import io
import logging
from flask import Blueprint, render_template, jsonify, send_file, current_app

from switch_dashboard.config import load_config
from switch_dashboard.services.backup_service import get_backup_service

logger = logging.getLogger("switch_dashboard.web.backups")

backups_bp = Blueprint("backups", __name__)


@backups_bp.route("/backups")
def backups_page():
    cfg = load_config()
    version = getattr(current_app, "version", "2.0.0")
    return render_template(
        "backups.html",
        title=cfg.get("title", "Switch Dashboard"),
        switches=cfg.get("switches", []),
        version=version,
    )


@backups_bp.route("/api/switches/<ip>/backup", methods=["POST"])
def backup_switch(ip: str):
    backup_service = get_backup_service()
    try:
        res = backup_service.backup_switch(ip)
        return jsonify(res)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except NotImplementedError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Backup failed for {ip}: {e}")
        return jsonify({"error": str(e)}), 500


@backups_bp.route("/api/switches/<ip>/reboot", methods=["POST"])
def reboot_switch(ip: str):
    backup_service = get_backup_service()
    try:
        backup_service.reboot_switch(ip)
        return jsonify({"status": "ok", "message": "Reboot command sent successfully"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except NotImplementedError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Reboot failed for {ip}: {e}")
        return jsonify({"error": str(e)}), 500


@backups_bp.route("/api/backups")
def get_backups():
    backup_service = get_backup_service()
    backups = backup_service.list_backups()
    return jsonify(backups)


@backups_bp.route("/api/backups/<filename>/download")
def download_backup_file(filename: str):
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400

    try:
        data = get_backup_service().read_backup(filename)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except Exception:
        logger.exception("Backup decryption failed")
        return jsonify({"error": "Unable to decrypt backup"}), 500
    return send_file(
        io.BytesIO(data),
        as_attachment=True,
        download_name=filename.removesuffix(".enc"),
    )


@backups_bp.route("/api/backups/<filename>", methods=["DELETE"])
def delete_backup_file(filename: str):
    backup_service = get_backup_service()
    try:
        deleted = backup_service.delete_backup(filename)
        if not deleted:
            return jsonify({"error": "File not found"}), 404
        return jsonify({"status": "ok"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
