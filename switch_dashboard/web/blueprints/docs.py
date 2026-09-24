from flask import Blueprint, render_template, current_app
from switch_dashboard.config import load_config

docs_bp = Blueprint("docs", __name__)


@docs_bp.route("/api-docs")
@docs_bp.route("/api/docs")
def api_docs_page():
    cfg = load_config()
    version = getattr(current_app, "version", "2.0.0")
    return render_template(
        "api_docs.html",
        title=cfg.get("title", "Switch Dashboard"),
        version=version,
    )
