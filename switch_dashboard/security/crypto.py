import base64
import json
import os
from typing import Any, Dict

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoConfigurationError(RuntimeError):
    pass


SECRET_FIELDS = frozenset({
    "password",
    "community",
    "snmp_community",
    "token_secret",
    "api_token",
    "api_secret",
    "secret",
    "snmp_auth_key",
    "snmp_priv_key",
    "auth_key",
    "priv_key",
    "private_key",
    "passphrase",
})


def _decode_key(value: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as exc:
        raise CryptoConfigurationError("DASHBOARD_ENCRYPTION_KEY must be URL-safe base64") from exc
    if len(key) != 32:
        raise CryptoConfigurationError("DASHBOARD_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def get_key() -> tuple[str, bytes]:
    encoded = os.environ.get("DASHBOARD_ENCRYPTION_KEY", "").strip()
    if not encoded and os.environ.get("TESTING") == "1":
        encoded = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    if not encoded:
        raise CryptoConfigurationError(
            "DASHBOARD_ENCRYPTION_KEY is required to store or read device secrets"
        )
    return os.environ.get("DASHBOARD_ENCRYPTION_KEY_ID", "v1"), _decode_key(encoded)


def _key_for_id(key_id: str) -> bytes:
    current_id, current_key = get_key()
    if key_id == current_id:
        return current_key
    for entry in os.environ.get("DASHBOARD_PREVIOUS_ENCRYPTION_KEYS", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        previous_id, separator, encoded = entry.partition(":")
        if separator and previous_id == key_id:
            return _decode_key(encoded)
    raise CryptoConfigurationError(f"No encryption key is configured for key ID {key_id!r}")


def encrypt_json(value: Dict[str, Any], context: str) -> tuple[str, str]:
    key_id, key = get_key()
    nonce = os.urandom(12)
    plaintext = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, context.encode("utf-8"))
    envelope = {
        "v": 1,
        "alg": "AES-256-GCM",
        "kid": key_id,
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }
    return key_id, json.dumps(envelope, separators=(",", ":"))


def encrypt_bytes(value: bytes, context: str) -> bytes:
    key_id, key = get_key()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, value, context.encode("utf-8"))
    header = json.dumps({
        "v": 1,
        "alg": "AES-256-GCM",
        "kid": key_id,
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
    }, separators=(",", ":")).encode("utf-8")
    return b"SWDBACKUP1\n" + header + b"\n" + ciphertext


def decrypt_bytes(value: bytes, context: str) -> bytes:
    try:
        magic, header_json, ciphertext = value.split(b"\n", 2)
        if magic != b"SWDBACKUP1":
            raise CryptoConfigurationError("Backup is not encrypted")
        header = json.loads(header_json)
        if header.get("v") != 1 or header.get("alg") != "AES-256-GCM":
            raise CryptoConfigurationError("Unsupported encrypted backup format")
        key = _key_for_id(str(header.get("kid", "")))
        nonce = base64.urlsafe_b64decode(header["nonce"])
        return AESGCM(key).decrypt(nonce, ciphertext, context.encode("utf-8"))
    except CryptoConfigurationError:
        raise
    except Exception as exc:
        raise CryptoConfigurationError("Unable to decrypt backup") from exc


def decrypt_json(envelope_json: str, context: str) -> Dict[str, Any]:
    try:
        envelope = json.loads(envelope_json)
        if envelope.get("v") != 1 or envelope.get("alg") != "AES-256-GCM":
            raise CryptoConfigurationError("Unsupported encrypted secret format")
        key = _key_for_id(str(envelope.get("kid", "")))
        nonce = base64.urlsafe_b64decode(envelope["nonce"])
        ciphertext = base64.urlsafe_b64decode(envelope["ciphertext"])
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, context.encode("utf-8"))
        result = json.loads(plaintext)
    except CryptoConfigurationError:
        raise
    except Exception as exc:
        raise CryptoConfigurationError("Unable to decrypt sensitive data") from exc
    if not isinstance(result, dict):
        raise CryptoConfigurationError("Encrypted secret payload is invalid")
    return result


def split_secrets(config: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    public = {}
    secrets = {}
    for key, value in config.items():
        if key in SECRET_FIELDS:
            if value not in (None, ""):
                secrets[key] = value
        else:
            public[key] = value
    return public, secrets


def redact_secrets(config: Dict[str, Any]) -> Dict[str, Any]:
    redacted = dict(config)
    for field in SECRET_FIELDS:
        if field in redacted:
            redacted[f"{field}_configured"] = redacted[field] not in (None, "")
            redacted.pop(field, None)
    return redacted


def redact_tree(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key in SECRET_FIELDS:
                redacted[f"{key}_configured"] = item not in (None, "")
            else:
                redacted[key] = redact_tree(item)
        return redacted
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    return value
