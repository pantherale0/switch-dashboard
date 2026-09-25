import base64
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
import threading
from collections import defaultdict, deque
from functools import lru_cache
from urllib.parse import urlencode, urlparse

import requests
from joserfc.jwk import KeySet, import_key
from joserfc.jwt import decode as jwt_decode
from flask import Blueprint, current_app, g, jsonify, make_response, redirect, render_template, request
from sqlalchemy import delete, select
from werkzeug.middleware.proxy_fix import ProxyFix

from switch_dashboard.storage.engine import get_db_session
from switch_dashboard.storage.models.auth import AuthSession, OidcLoginTransaction, SecurityAuditEvent


logger = logging.getLogger("switch_dashboard.web.auth")
auth_bp = Blueprint("auth", __name__)

ROLE_LEVEL = {"viewer": 10, "operator": 20, "admin": 30}
PUBLIC_ENDPOINTS = {"auth.login", "auth.callback", "auth.health", "static"}
ADMIN_ENDPOINTS = {
    "config_bp.config_page",
    "config_bp.api_get_devices",
    "config_bp.api_add_device",
    "config_bp.api_update_device",
    "config_bp.api_delete_device",
    "config_bp.manage_template",
    "config_bp.api_settings",
    "config_bp.api_device_types_raw",
    "config_bp.api_vendors",
    "config_bp.update_oui_api",
    "logs.logs_page",
    "logs.api_logs",
    "logs.api_logs_level",
    "logs.api_logs_clear",
    "logs.api_logs_download",
    "backups.download_backup_file",
    "backups.delete_backup_file",
    "backups.reboot_switch",
    "metrics.api_reset",
    "scanner.api_scanner_delete_host_history",
    "scanner.api_scanner_clear_all_history",
}
OPERATOR_ENDPOINTS = {
    "dashboard.refresh_mac",
    "backups.backups_page",
    "backups.backup_switch",
    "backups.get_backups",
    "protocols.validate_protocol_config",
    "protocols.test_device_connection",
    "metrics.api_notes",
    "clients.edit_client_meta",
    "topology.api_clients_update_host",
    "topology.api_clients_update_type",
    "topology.api_clients_update_passthrough",
    "topology.api_clients_delete",
    "topology.api_clients_import_csv",
    "scanner.api_scanner_known",
    "scanner.api_scanner_update_host",
    "scanner.api_scanner_delete_host",
    "config_bp.api_get_upstream_candidates",
    "protocols.detect_upstream",
}
VIEWER_ENDPOINTS = {
    "auth.logout",
    "dashboard.dashboard",
    "dashboard.api_switches",
    "dashboard.get_transceiver",
    "dashboard.get_switch_image",
    "topology.network_map",
    "topology.api_topology",
    "metrics.api_speeds",
    "metrics.api_history",
    "clients.clients_view",
    "clients.get_subnets",
    "clients.get_summary",
    "clients.get_clients_list",
    "clients.get_client_details",
    "scanner.scanner_dashboard",
    "scanner.scanner_history",
    "scanner.api_scanner_hosts",
    "scanner.api_scanner_history",
    "protocols.api_protocols",
    "docs.api_docs_page",
    "config_bp.api_device_types",
}
_rate_lock = threading.Lock()
_request_times = defaultdict(deque)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def auth_disabled() -> bool:
    requested = _env_bool("AUTH_DISABLED")
    testing = bool(current_app.testing or os.environ.get("TESTING") == "1")
    return requested and testing


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_return_to(value: str) -> str:
    parsed = urlparse(value or "/")
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@lru_cache(maxsize=1)
def _discovery() -> dict:
    issuer = os.environ.get("OIDC_ISSUER", "").rstrip("/")
    if not issuer:
        raise RuntimeError("OIDC_ISSUER is not configured")
    response = requests.get(f"{issuer}/.well-known/openid-configuration", timeout=10)
    response.raise_for_status()
    document = response.json()
    if document.get("issuer", "").rstrip("/") != issuer:
        raise RuntimeError("OIDC discovery issuer mismatch")
    return document


def _oidc_ready() -> bool:
    return bool(os.environ.get("OIDC_ISSUER") and os.environ.get("OIDC_CLIENT_ID"))


def _role_from_claims(claims: dict) -> str | None:
    claim_name = os.environ.get("OIDC_ROLES_CLAIM", "groups")
    values = claims.get(claim_name, [])
    if isinstance(values, str):
        values = [values]
    values = {str(value) for value in values}
    mappings = (
        ("admin", os.environ.get("OIDC_ADMIN_GROUP", "switch-dashboard-admin")),
        ("operator", os.environ.get("OIDC_OPERATOR_GROUP", "switch-dashboard-operator")),
        ("viewer", os.environ.get("OIDC_VIEWER_GROUP", "switch-dashboard-viewer")),
    )
    for role, group in mappings:
        if group and group in values:
            return role
    return None


def _required_role() -> str:
    endpoint = request.endpoint or ""
    if endpoint in {"config_bp.api_config_settings", "config_bp.api_settings"}:
        return "viewer" if request.method in {"GET", "HEAD"} else "operator"
    if endpoint in VIEWER_ENDPOINTS:
        return "viewer"
    if endpoint in ADMIN_ENDPOINTS:
        return "admin"
    if endpoint in OPERATOR_ENDPOINTS:
        return "operator"
    return "admin"


def _unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication_required"}), 401
    return redirect(f"/auth/login?next={request.full_path.rstrip('?')}")


def _rate_limited() -> bool:
    if request.endpoint in {"auth.health", "static"}:
        return False
    now = time.monotonic()
    session_token = request.cookies.get(current_app.config.get("AUTH_COOKIE_NAME", ""), "")
    key = _hash(session_token) if session_token else (request.remote_addr or "unknown")
    limit = 20 if request.endpoint == "auth.login" else int(os.environ.get("REQUESTS_PER_MINUTE", "180"))
    bucket_key = (key, request.endpoint == "auth.login")
    with _rate_lock:
        bucket = _request_times[bucket_key]
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= limit:
            return True
        bucket.append(now)
    return False


def _audit(action: str, result: str, target: str = "") -> None:
    user = getattr(g, "current_user", None) or {}
    try:
        with get_db_session() as session:
            session.add(SecurityAuditEvent(
                id=str(uuid.uuid4()),
                created_at=time.time(),
                subject=str(user.get("subject", "anonymous")),
                username=str(user.get("username", "anonymous")),
                action=action[:255],
                target=target[:2048],
                result=result[:32],
                source_ip=(request.remote_addr or "")[:64],
            ))
    except Exception:
        logger.exception("Unable to persist security audit event")


def security_before_request():
    if _rate_limited():
        return jsonify({"error": "rate_limit_exceeded"}), 429
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if auth_disabled():
        g.current_user = {"subject": "test", "username": "test", "role": "admin"}
        g.csrf_token = "test-csrf-token"
        return None
    if not _oidc_ready():
        return jsonify({"error": "authentication_not_configured"}), 503

    raw_token = request.cookies.get(current_app.config["AUTH_COOKIE_NAME"], "")
    if not raw_token:
        return _unauthorized()
    now = time.time()
    with get_db_session() as session:
        auth_session = session.scalar(
            select(AuthSession).where(
                AuthSession.token_hash == _hash(raw_token),
                AuthSession.expires_at > now,
            )
        )
        if auth_session is None:
            return _unauthorized()
        if now - auth_session.last_seen_at > current_app.config["AUTH_IDLE_TIMEOUT"]:
            session.delete(auth_session)
            return _unauthorized()
        auth_session.last_seen_at = now
        g.current_user = {
            "subject": auth_session.subject,
            "username": auth_session.username,
            "role": auth_session.role,
        }
        g.csrf_token = auth_session.csrf_token

    required = _required_role()
    if ROLE_LEVEL.get(g.current_user["role"], 0) < ROLE_LEVEL[required]:
        _audit("authorization.denied", "forbidden", request.path)
        return jsonify({"error": "forbidden", "required_role": required}), 403

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if (
            request.path.startswith("/api/")
            and request.content_length
            and not request.is_json
            and request.endpoint != "topology.api_clients_import_csv"
        ):
            return jsonify({"error": "content_type_must_be_application_json"}), 415
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not supplied or not secrets.compare_digest(supplied, g.csrf_token):
            _audit("csrf.denied", "forbidden", request.path)
            return jsonify({"error": "invalid_csrf_token"}), 403
    return None


@auth_bp.route("/healthz")
def health():
    return jsonify({"status": "ok"})


@auth_bp.route("/auth/login")
def login():
    if auth_disabled():
        return redirect("/")
    if not _oidc_ready():
        return render_template("auth_error.html", message="OIDC authentication is not configured."), 503
    try:
        discovery = _discovery()
        state = secrets.token_urlsafe(32)
        browser_token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        now = time.time()
        with get_db_session() as session:
            session.execute(delete(OidcLoginTransaction).where(OidcLoginTransaction.expires_at <= now))
            session.add(OidcLoginTransaction(
                state_hash=_hash(state),
                browser_token_hash=_hash(browser_token),
                nonce=nonce,
                code_verifier=verifier,
                return_to=_safe_return_to(request.args.get("next", "/")),
                expires_at=now + 600,
            ))
        params = {
            "response_type": "code",
            "client_id": os.environ["OIDC_CLIENT_ID"],
            "redirect_uri": os.environ.get("OIDC_REDIRECT_URI", request.url_root.rstrip("/") + "/auth/callback"),
            "scope": os.environ.get("OIDC_SCOPES", "openid profile email groups"),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        response = redirect(f"{discovery['authorization_endpoint']}?{urlencode(params)}")
        response.set_cookie(
            "sd_oidc_txn", browser_token, max_age=600, httponly=True,
            secure=current_app.config["AUTH_COOKIE_SECURE"], samesite="Lax", path="/auth/callback",
        )
        return response
    except Exception:
        logger.exception("Unable to begin OIDC login")
        return render_template("auth_error.html", message="Authentication provider is unavailable."), 503


@auth_bp.route("/auth/callback")
def callback():
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    browser_token = request.cookies.get("sd_oidc_txn", "")
    if not state or not code or not browser_token:
        return render_template("auth_error.html", message="Invalid authentication response."), 400
    now = time.time()
    try:
        with get_db_session() as session:
            transaction = session.get(OidcLoginTransaction, _hash(state))
            if (
                transaction is None
                or transaction.expires_at <= now
                or not secrets.compare_digest(transaction.browser_token_hash, _hash(browser_token))
            ):
                return render_template("auth_error.html", message="Authentication transaction expired or invalid."), 400
            nonce = transaction.nonce
            verifier = transaction.code_verifier
            return_to = transaction.return_to
            session.delete(transaction)

        discovery = _discovery()
        token_data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": os.environ.get("OIDC_REDIRECT_URI", request.url_root.rstrip("/") + "/auth/callback"),
                "client_id": os.environ["OIDC_CLIENT_ID"],
                "code_verifier": verifier,
        }
        client_secret = os.environ.get("OIDC_CLIENT_SECRET", "")
        auth_method = os.environ.get(
            "OIDC_TOKEN_AUTH_METHOD", "client_secret_basic" if client_secret else "none"
        )
        token_auth = None
        if auth_method == "client_secret_basic":
            token_auth = (os.environ["OIDC_CLIENT_ID"], client_secret)
        elif auth_method == "client_secret_post":
            token_data["client_secret"] = client_secret
        elif auth_method != "none":
            raise RuntimeError("Unsupported OIDC_TOKEN_AUTH_METHOD")
        token_response = requests.post(
            discovery["token_endpoint"],
            data=token_data,
            auth=token_auth,
            timeout=10,
        )
        token_response.raise_for_status()
        id_token = token_response.json().get("id_token")
        if not id_token:
            raise RuntimeError("OIDC response did not include an ID token")
        jwks_response = requests.get(discovery["jwks_uri"], timeout=10)
        jwks_response.raise_for_status()
        jwks_keys = [
            import_key(entry)
            for entry in jwks_response.json().get("keys", [])
            if isinstance(entry, dict) and entry
        ]
        if not jwks_keys:
            raise RuntimeError("OIDC JWKS contained no usable keys")
        allowed_algs = {
            "RS256", "RS384", "RS512",
            "ES256", "ES384", "ES512",
            "PS256", "PS384", "PS512",
        }
        token = jwt_decode(id_token, KeySet(jwks_keys), algorithms=allowed_algs)
        claims = token.claims
        now = time.time()
        leeway = 60
        if claims.get("iss") != discovery["issuer"]:
            raise RuntimeError("OIDC issuer claim mismatch")
        if claims.get("nonce") != nonce:
            raise RuntimeError("OIDC nonce claim mismatch")
        if os.environ["OIDC_CLIENT_ID"] not in audiences:
            raise RuntimeError("OIDC audience claim mismatch")
        for claim in ("exp", "nbf", "iat"):
            value = claims.get(claim)
            if isinstance(value, (int, float)):
                if claim == "exp" and now - leeway > value:
                    raise RuntimeError("OIDC token expired")
                if claim in ("nbf", "iat") and value - leeway > now:
                    raise RuntimeError(f"OIDC {claim} claim not valid yet")
        authorized_party = claims.get("azp")
        if len(audiences) > 1 and authorized_party != os.environ["OIDC_CLIENT_ID"]:
            raise RuntimeError("OIDC authorized-party claim mismatch")
        if authorized_party and authorized_party != os.environ["OIDC_CLIENT_ID"]:
            raise RuntimeError("OIDC authorized-party claim mismatch")
        role = _role_from_claims(claims)
        if role is None:
            return render_template("auth_error.html", message="Your identity is not assigned an application role."), 403
        subject = str(claims["sub"])
        username_claim = os.environ.get("OIDC_USERNAME_CLAIM", "preferred_username")
        username = str(claims.get(username_claim) or claims.get("email") or subject)
        raw_session = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        lifetime = current_app.config["AUTH_SESSION_LIFETIME"]
        with get_db_session() as session:
            session.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
            session.add(AuthSession(
                token_hash=_hash(raw_session), subject=subject, username=username, role=role,
                csrf_token=csrf_token, created_at=now, last_seen_at=now, expires_at=now + lifetime,
            ))
        g.current_user = {"subject": subject, "username": username, "role": role}
        _audit("authentication.login", "success")
        response = redirect(return_to)
        response.delete_cookie("sd_oidc_txn", path="/auth/callback")
        response.set_cookie(
            current_app.config["AUTH_COOKIE_NAME"], raw_session, max_age=lifetime,
            httponly=True, secure=current_app.config["AUTH_COOKIE_SECURE"], samesite="Lax", path="/",
        )
        return response
    except Exception:
        logger.exception("OIDC callback failed")
        return render_template("auth_error.html", message="Authentication failed."), 401


@auth_bp.route("/auth/logout", methods=["POST"])
def logout():
    raw_token = request.cookies.get(current_app.config["AUTH_COOKIE_NAME"], "")
    if raw_token:
        with get_db_session() as session:
            session.execute(delete(AuthSession).where(AuthSession.token_hash == _hash(raw_token)))
    _audit("authentication.logout", "success")
    response = make_response(redirect("/auth/login"))
    response.delete_cookie(current_app.config["AUTH_COOKIE_NAME"], path="/")
    return response


def init_auth(app):
    trusted_proxy_count = int(os.environ.get("TRUSTED_PROXY_COUNT", "0"))
    if trusted_proxy_count:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trusted_proxy_count,
            x_proto=trusted_proxy_count,
            x_host=trusted_proxy_count,
        )
    app.config["AUTH_COOKIE_NAME"] = os.environ.get("AUTH_COOKIE_NAME", "switch_dashboard_session")
    app.config["AUTH_COOKIE_SECURE"] = not _env_bool("ALLOW_INSECURE_HTTP")
    app.config["AUTH_SESSION_LIFETIME"] = int(os.environ.get("AUTH_SESSION_LIFETIME", "3600"))
    app.config["AUTH_IDLE_TIMEOUT"] = int(os.environ.get("AUTH_IDLE_TIMEOUT", "1800"))
    app.register_blueprint(auth_bp)
    app.before_request(security_before_request)

    @app.context_processor
    def security_context():
        return {
            "current_user": getattr(g, "current_user", None),
            "csrf_token": getattr(g, "csrf_token", ""),
        }

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cache-Control", "no-store")
        if app.config["AUTH_COOKIE_SECURE"]:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.endpoint not in {"auth.logout", "auth.callback"}
            and getattr(g, "current_user", None)
            and not auth_disabled()
        ):
            _audit(
                f"request.{request.endpoint or 'unknown'}",
                "success" if response.status_code < 400 else "failure",
                request.path,
            )
        return response
