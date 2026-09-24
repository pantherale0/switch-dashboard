import pytest
import voluptuous as vol

from switch_dashboard.protocols.base import voluptuous_to_ui_schema
from switch_dashboard.protocols.registry import ProtocolRegistry


def test_voluptuous_to_ui_schema_conversion():
    schema = vol.Schema({
        vol.Required("username", default="admin", description="Login Username"): str,
        vol.Required("password", default="", description="Login Password"): str,
        vol.Optional("port", default=80, description="Web Port"): vol.Coerce(int),
        vol.Optional("version", default="v2", description="Protocol Version"): vol.In(["v1", "v2", "v3"]),
    })

    ui_schema = voluptuous_to_ui_schema(schema)
    assert len(ui_schema) == 4

    by_name = {f["name"]: f for f in ui_schema}
    
    assert by_name["username"]["type"] == "text"
    assert by_name["username"]["required"] is True
    assert by_name["username"]["default"] == "admin"
    assert by_name["username"]["description"] == "Login Username"

    assert by_name["password"]["type"] == "password"
    assert by_name["password"]["required"] is True

    assert by_name["port"]["type"] == "number"
    assert by_name["port"]["required"] is False
    assert by_name["port"]["default"] == 80

    assert by_name["version"]["type"] == "select"
    assert by_name["version"]["choices"] == ["v1", "v2", "v3"]
    assert by_name["version"]["default"] == "v2"


def test_validate_config_http_hc():
    valid_cfg = {
        "username": "admin",
        "password": "secretpassword",
        "model": "HC-SWTGW218AS",
        "http_timeout": 30,
    }
    is_valid, err, coerced = ProtocolRegistry.validate_config("http_hc", valid_cfg)
    assert is_valid is True
    assert err is None
    assert coerced["username"] == "admin"
    assert coerced["password"] == "secretpassword"
    assert coerced["http_timeout"] == 30

    # Missing required password
    invalid_cfg = {"username": "admin"}
    is_valid, err, _ = ProtocolRegistry.validate_config("http_hc", invalid_cfg)
    assert is_valid is False
    assert "password" in err.lower()


def test_validate_config_snmp():
    valid_cfg = {
        "community": "public",
        "snmp_port": "161",  # Coerced to int
        "version": "2c",
    }
    is_valid, err, coerced = ProtocolRegistry.validate_config("snmp", valid_cfg)
    assert is_valid is True
    assert coerced["snmp_port"] == 161
    assert coerced["version"] == "2c"

    # Invalid version
    invalid_cfg = {
        "community": "public",
        "version": "99",
    }
    is_valid, err, _ = ProtocolRegistry.validate_config("snmp", invalid_cfg)
    assert is_valid is False
    assert "version" in err.lower()


def test_validate_config_fritzbox():
    valid_cfg = {
        "password": "fritzpassword",
        "protocol_port": 49000,
    }
    is_valid, err, coerced = ProtocolRegistry.validate_config("fritzbox", valid_cfg)
    assert is_valid is True
    assert coerced["password"] == "fritzpassword"
    assert coerced["protocol_port"] == 49000

    # Missing required password
    invalid_cfg = {"username": "admin"}
    is_valid, err, _ = ProtocolRegistry.validate_config("fritzbox", invalid_cfg)
    assert is_valid is False
    assert "password" in err.lower()


def test_protocol_registry_list_protocols_includes_schema():
    protos = ProtocolRegistry.list_protocols()
    assert len(protos) >= 4

    proto_dict = {p["name"]: p for p in protos}
    assert "http_hc" in proto_dict
    assert "snmp" in proto_dict
    assert "fritzbox" in proto_dict
    assert "ovs" in proto_dict

    for name in ["http_hc", "snmp", "fritzbox", "ovs"]:
        p = proto_dict[name]
        assert "config_schema" in p
        assert isinstance(p["config_schema"], list)
        assert len(p["config_schema"]) > 0

    proxmox_fields = {
        field["name"]: field for field in proto_dict["proxmox"]["config_schema"]
    }
    assert proxmox_fields["token_id"]["type"] == "text"
    assert proxmox_fields["token_secret"]["type"] == "password"
