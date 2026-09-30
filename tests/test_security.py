import base64
import hashlib
import os
import secrets
import tempfile
import time
from types import SimpleNamespace

from joserfc.jwk import generate_key
from joserfc.jwt import encode

from switch_dashboard.config import get_default_config, load_config, save_config, set_data_dir
from switch_dashboard.security.crypto import decrypt_bytes, encrypt_bytes
from switch_dashboard.services.backup_service import BackupService
from switch_dashboard.storage.engine import get_db_session
from switch_dashboard.storage.models.auth import AuthSession, OidcLoginTransaction
from switch_dashboard.storage.models.config import DeviceConfig, DeviceSecret
from switch_dashboard.web import create_app
from switch_dashboard.web import auth as auth_module


def _add_session(client, role):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with get_db_session() as session:
        session.add(AuthSession(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            subject=f"test-{role}",
            username=f"test-{role}",
            role=role,
            csrf_token="csrf-test",
            created_at=now,
            last_seen_at=now,
            expires_at=now + 3600,
        ))
    client.set_cookie("switch_dashboard_session", token)


def _secured_app(monkeypatch):
    temp_dir = tempfile.TemporaryDirectory(prefix="switch_dashboard_security_")
    set_data_dir(temp_dir.name)
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setenv("OIDC_ISSUER", "https://id.example.test")
    monkeypatch.setenv("OIDC_CLIENT_ID", "switch-dashboard")
    monkeypatch.setenv(
        "DASHBOARD_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode(),
    )
    save_config(get_default_config())
    app = create_app(start_background_workers=False)
    app.testing = True
    return temp_dir, app


def test_anonymous_api_is_rejected(monkeypatch):
    temp_dir, app = _secured_app(monkeypatch)
    try:
        response = app.test_client().get("/api/switches")
        assert response.status_code == 401
    finally:
        temp_dir.cleanup()


def test_role_and_csrf_enforcement(monkeypatch):
    temp_dir, app = _secured_app(monkeypatch)
    try:
        viewer = app.test_client()
        _add_session(viewer, "viewer")
        assert viewer.get("/api/switches").status_code == 200
        assert viewer.get("/api/devices").status_code == 403

        operator = app.test_client()
        _add_session(operator, "operator")
        payload = {"key": "192.168.1.1:1", "note": "secured"}
        assert operator.post("/api/notes", json=payload).status_code == 403
        assert operator.post(
            "/api/notes", json=payload, headers={"X-CSRF-Token": "csrf-test"}
        ).status_code == 200
    finally:
        temp_dir.cleanup()


def test_oidc_callback_uses_valid_jwks_key_and_ignores_unusable_entries(monkeypatch):
    temp_dir, app = _secured_app(monkeypatch)
    try:
        client = app.test_client()
        state = "oidc-state"
        browser_token = "oidc-browser-token"
        nonce = "oidc-nonce"
        now = time.time()
        with get_db_session() as session:
            session.add(OidcLoginTransaction(
                state_hash=hashlib.sha256(state.encode()).hexdigest(),
                browser_token_hash=hashlib.sha256(browser_token.encode()).hexdigest(),
                nonce=nonce,
                code_verifier="code-verifier",
                return_to="/",
                expires_at=now + 600,
            ))

        private_key = generate_key("RSA", 2048, parameters={"kid": "signing-key"})
        id_token = encode(
            {"alg": "RS256", "kid": "signing-key"},
            {
                "iss": "https://id.example.test",
                "sub": "oidc-user",
                "aud": "switch-dashboard",
                "nonce": nonce,
                "iat": now,
                "exp": now + 300,
                "groups": ["switch-dashboard-admin"],
            },
            private_key,
            algorithms={"RS256"},
        )
        jwks = {
            "keys": [
                {"kty": "UNKNOWN", "kid": "future-key"},
                private_key.as_dict(),
            ]
        }
        monkeypatch.setattr(auth_module, "_discovery", lambda: {
            "issuer": "https://id.example.test",
            "token_endpoint": "https://id.example.test/token",
            "jwks_uri": "https://id.example.test/jwks",
        })
        monkeypatch.setattr(auth_module.requests, "post", lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"id_token": id_token},
        ))
        monkeypatch.setattr(auth_module.requests, "get", lambda *args, **kwargs: SimpleNamespace(
            url="https://id.example.test/jwks",
            raise_for_status=lambda: None,
            json=lambda: jwks,
        ))
        client.set_cookie("sd_oidc_txn", browser_token)

        response = client.get(f"/auth/callback?state={state}&code=authorization-code")

        assert response.status_code == 302
        assert response.headers["Location"] == "/"
        assert any(
            "switch_dashboard_session" in cookie
            for cookie in response.headers.getlist("Set-Cookie")
        )
    finally:
        temp_dir.cleanup()


def test_oidc_jwks_rejects_empty_or_invalid_key_sets():
    for jwks in ({}, {"keys": []}, {"keys": [{"kty": "UNKNOWN"}]}):
        try:
            auth_module._oidc_key_set(jwks)
        except RuntimeError as exc:
            assert "usable signing keys" in str(exc) or "keys array" in str(exc)
        else:
            raise AssertionError("Invalid JWKS unexpectedly produced a key set")


def test_device_secrets_are_encrypted_and_redacted(monkeypatch):
    temp_dir, app = _secured_app(monkeypatch)
    try:
        cfg = get_default_config()
        cfg["devices"] = [{
            "id": "secure-device",
            "name": "Secure device",
            "ip": "192.168.1.10",
            "protocol": "http_hc",
            "management_type": "managed",
            "username": "admin",
            "password": "correct horse battery staple",
        }]
        assert save_config(cfg)
        assert load_config()["devices"][0]["password"] == "correct horse battery staple"
        with get_db_session() as session:
            device = session.get(DeviceConfig, "secure-device")
            encrypted = session.get(DeviceSecret, "secure-device")
            assert device.password == ""
            assert "correct horse battery staple" not in encrypted.ciphertext

        admin = app.test_client()
        _add_session(admin, "admin")
        response = admin.get("/api/devices").get_json()["devices"][0]
        assert "password" not in response
        assert response["password_configured"] is True
    finally:
        temp_dir.cleanup()


def test_backup_encryption_round_trip(monkeypatch):
    monkeypatch.setenv(
        "DASHBOARD_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode(),
    )
    plaintext = b"sensitive switch configuration"
    encrypted = encrypt_bytes(plaintext, "backup:test.bin.enc")
    assert plaintext not in encrypted
    assert decrypt_bytes(encrypted, "backup:test.bin.enc") == plaintext


def test_legacy_backup_is_migrated(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "DASHBOARD_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode(),
    )
    legacy = tmp_path / "switch_cfg_192-168-1-10_20260925_1200.bin"
    legacy.write_bytes(b"legacy secret backup")
    service = BackupService(str(tmp_path))
    assert not legacy.exists()
    encrypted_name = f"{legacy.name}.enc"
    assert service.read_backup(encrypted_name) == b"legacy secret backup"
    assert b"legacy secret backup" not in (tmp_path / encrypted_name).read_bytes()


def test_config_export_redacts_all_secret_collections(monkeypatch):
    temp_dir, app = _secured_app(monkeypatch)
    try:
        cfg = get_default_config()
        cfg["devices"] = [{
            "id": "redaction-device",
            "name": "Redaction device",
            "ip": "192.168.1.11",
            "management_type": "managed",
            "password": "device-password",
        }]
        cfg["proxmox_nodes"] = [{"token_secret": "node-token"}]
        cfg["unifi_nodes"] = [{"password": "node-password"}]
        assert save_config(cfg)
        exported = (os.path.join(temp_dir.name, "config.json"))
        with open(exported, "r", encoding="utf-8") as f:
            contents = f.read()
        assert "device-password" not in contents
        assert "node-token" not in contents
        assert "node-password" not in contents
    finally:
        temp_dir.cleanup()
