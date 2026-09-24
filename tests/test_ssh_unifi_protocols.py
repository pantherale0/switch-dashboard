import json
from unittest.mock import Mock, patch

import paramiko
import pytest

from switch_dashboard.protocols.base import Capability
from switch_dashboard.protocols.drivers.ovs import OVSProtocol
from switch_dashboard.protocols.drivers.ssh import SSHProtocol
from switch_dashboard.protocols.drivers.unifi import UnifiProtocol
from switch_dashboard.protocols.drivers.unifi.driver import AUTO_MAC_TABLE_COMMAND
from switch_dashboard.protocols.registry import ProtocolRegistry
from switch_dashboard.config import _infer_protocol


def test_ssh_protocol_is_registered_and_selectable():
    protocols = {item["name"]: item for item in ProtocolRegistry.list_protocols()}
    assert "ssh" in protocols
    assert "unifi" in protocols
    assert {"username", "scrape_command", "strict_host_key"}.issubset(
        {field["name"] for field in protocols["ssh"]["config_schema"]}
    )

    driver = ProtocolRegistry.create({
        "ip": "192.0.2.10",
        "protocol": "ssh",
        "username": "monitor",
        "scrape_command": "device-status --json",
    })
    assert isinstance(driver, SSHProtocol)
    assert driver.has_capability(Capability.NEIGHBORS)


def test_ssh_schema_requires_command_and_coerces_numbers():
    valid, error, config = ProtocolRegistry.validate_config("ssh", {
        "username": "monitor",
        "scrape_command": "status-json",
        "ssh_port": "2222",
        "ssh_timeout": "8",
    })
    assert valid is True
    assert error is None
    assert config["ssh_port"] == 2222
    assert config["ssh_timeout"] == 8

    valid, error, _ = ProtocolRegistry.validate_config("ssh", {"username": "monitor"})
    assert valid is False
    assert "scrape_command" in error


def test_ssh_host_key_policy_only_loads_known_hosts_in_strict_mode():
    strict_driver = SSHProtocol({
        "ip": "192.0.2.30",
        "username": "monitor",
        "scrape_command": "status-json",
        "strict_host_key": True,
    })
    relaxed_driver = SSHProtocol({
        "ip": "192.0.2.31",
        "username": "monitor",
        "scrape_command": "status-json",
        "strict_host_key": False,
    })

    with patch("switch_dashboard.protocols.drivers.ssh.driver.paramiko.SSHClient") as client_cls:
        strict_client = client_cls.return_value
        strict_driver._create_client()
        strict_client.load_system_host_keys.assert_called_once_with()
        assert isinstance(strict_client.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)

    with patch("switch_dashboard.protocols.drivers.ssh.driver.paramiko.SSHClient") as client_cls:
        relaxed_client = client_cls.return_value
        relaxed_driver._create_client()
        relaxed_client.load_system_host_keys.assert_not_called()
        assert isinstance(relaxed_client.set_missing_host_key_policy.call_args.args[0], paramiko.AutoAddPolicy)


def test_ssh_strict_mode_reports_actionable_stale_host_key_error():
    driver = SSHProtocol({
        "ip": "192.0.2.32",
        "username": "monitor",
        "scrape_command": "status-json",
        "strict_host_key": True,
    })
    client = Mock()
    client.connect.side_effect = paramiko.BadHostKeyException(
        "192.0.2.32", Mock(), Mock()
    )
    driver._create_client = Mock(return_value=client)

    with pytest.raises(RuntimeError, match=r"ssh-keygen -R '192\.0\.2\.32'"):
        driver._connect()
    client.close.assert_called_once_with()


def test_generic_ssh_json_commands_are_normalized():
    driver = SSHProtocol({
        "ip": "192.0.2.11",
        "name": "Custom switch",
        "model": "custom-os",
        "username": "monitor",
        "scrape_command": "status-json",
        "mac_table_command": "fdb-json",
        "neighbors_command": "lldp-json",
        "port_count": 2,
    })
    outputs = {
        "status-json": json.dumps({
            "ports": [{"port": 1, "status": "up", "speed": "1G", "tx_bytes": 12}],
        }),
        "fdb-json": json.dumps([{"mac": "00:11:22:33:44:55", "port": "1", "vlan": "1"}]),
        "lldp-json": json.dumps([{"local_port": "1", "remote_system_name": "core"}]),
    }
    driver.execute_command = Mock(side_effect=lambda command: outputs[command])

    data = driver.scrape()
    assert data["status"] == "online"
    assert data["name"] == "Custom switch"
    assert data["ports"][0]["port"] == "1"
    assert data["ports"][0]["link"] == "Link Up"
    assert data["ports"][0]["rx_bytes"] == 0
    assert driver.scrape_mac_table()[0]["port"] == "1"
    assert driver.scrape_neighbors()[0]["remote_system_name"] == "core"


def test_generic_ssh_invalid_json_returns_offline_ports():
    driver = SSHProtocol({
        "ip": "192.0.2.12",
        "username": "monitor",
        "scrape_command": "bad-status",
        "port_count": 2,
    })
    driver.execute_command = Mock(return_value="not json")

    data = driver.scrape()
    assert data["status"] == "offline"
    assert "valid JSON" in data["error"]
    assert [port["status"] for port in data["ports"]] == ["down", "down"]


def test_unifi_mca_dump_parsing_and_command_fallback():
    mca_dump = {
        "hostname": "office-usw",
        "model": "USW-Lite-8-PoE",
        "version": "7.1.26",
        "uptime": 90061,
        "mac": "001122aabbcc",
        "port_table": [
            {
                "port_idx": 1,
                "name": "Port 1",
                "up": True,
                "speed": 1000,
                "full_duplex": True,
                "tx_bytes": 1234,
                "rx_bytes": 5678,
                "tx_packets": 10,
                "rx_packets": 20,
            },
            {"port_idx": 2, "name": "Port 2", "up": False, "speed": 1000},
        ],
        "lldp_table": [{
            "port_idx": 1,
            "chassis_id": "aa-bb-cc-dd-ee-ff",
            "port_id": "Gi1/0/1",
            "system_name": "core-switch",
        }],
    }
    mac_output = "VID MAC Address         Port Type\n20  10:20:30:40:50:60 0/1 Dynamic\n"
    driver = UnifiProtocol({
        "ip": "192.0.2.20",
        "protocol": "unifi",
        "username": "ubnt",
        "password": "secret",
    })
    driver.execute_command = Mock(side_effect=[json.dumps(mca_dump), mac_output])

    data = driver.scrape()
    assert data["status"] == "online"
    assert data["name"] == "office-usw"
    assert data["model"] == "USW-Lite-8-PoE"
    assert data["uptime"] == "1 days, 01:01:01"
    assert data["mac"] == "00:11:22:AA:BB:CC"
    assert data["ports"][0]["speed"] == "1G"
    assert data["ports"][0]["tx_bytes"] == 1234
    assert data["ports"][1]["status"] == "down"
    assert data["mac_table"] == [{
        "mac": "10:20:30:40:50:60",
        "port": "0/1",
        "vlan": "20",
        "type": "dynamic",
    }]
    assert data["neighbors"][0]["remote_chassis_id"] == "AA:BB:CC:DD:EE:FF"
    assert driver.scrape_mac_table() == data["mac_table"]
    assert driver.scrape_neighbors() == data["neighbors"]
    assert driver.execute_command.call_count == 2
    assert driver.execute_command.call_args_list[1].args[0] == AUTO_MAC_TABLE_COMMAND


def test_unifi_interface_table_and_embedded_mac_table():
    dump = {
        "device_model": "UAP-AC-Pro",
        "device_version": "6.6.77",
        "if_table": [
            {"name": "lo", "up": True},
            {"name": "eth0", "up": "up", "speed": 1000, "rx_bytes": "42"},
        ],
        "station_table": [{"mac": "aabbccddeeff", "vid": 30}],
    }
    driver = UnifiProtocol({"ip": "192.0.2.21", "username": "ubnt"})
    driver.execute_command = Mock(return_value=json.dumps(dump))

    data = driver.scrape()
    assert [port["port"] for port in data["ports"]] == ["eth0", "WLAN"]
    assert data["ports"][0]["rx_bytes"] == 42
    assert data["mac_table"][0] == {
        "mac": "AA:BB:CC:DD:EE:FF",
        "port": "WLAN",
        "vlan": "30",
        "type": "dynamic",
    }
    assert driver.execute_command.call_count == 1


def test_unifi_ap_with_no_clients_skips_switch_mac_command():
    dump = {
        "type": "uap",
        "model": "UAP-U7-Pro",
        "if_table": [{"name": "eth0", "up": True, "speed": 2500}],
        "station_table": [],
    }
    driver = UnifiProtocol({"ip": "192.0.2.23", "username": "ubnt"})
    driver.execute_command = Mock(return_value=json.dumps(dump))

    data = driver.scrape()
    assert data["status"] == "online"
    assert data["mac_table"] == []
    assert driver.execute_command.call_count == 1


def test_unifi_real_ap_dump_shape_parses_vaps_clients_and_lldp():
    dump = {
        "hostname": "LandingAP",
        "model": "U7LR",
        "model_display": "UAP-AC-LR",
        "version": "6.8.2.15592",
        "uptime": 77694,
        "mac": "f4:92:bf:29:9d:0f",
        "radio_table": [{"name": "wifi0"}, {"name": "wifi1"}],
        "if_table": [{
            "name": "eth0",
            "num_port": 1,
            "up": True,
            "speed": 100,
            "full_duplex": True,
            "rx_bytes": 3334898698,
            "tx_bytes": 597357690,
        }],
        "lldp_table": [{
            "chassis_id": "10.10.50.124",
            "chassis_id_subtype": "ip",
            "local_port_idx": 1,
            "local_port_name": "eth0",
            "port_id": "a4:93:4c:fe:f8:c0",
            "power_allocated": 6500,
            "power_requested": 12950,
        }],
        "vap_table": [{
            "name": "wifi1ap8",
            "essid": "Office-IOT",
            "radio": "na",
            "state": "RUN",
            "up": True,
            "num_sta": 1,
            "channel": 104,
            "tx_bytes": 1046229223,
            "rx_bytes": 26022656876,
            "tx_packets": 12430088,
            "rx_packets": 19952758,
            "sta_table": [{
                "mac": "bc:fd:0c:b6:6e:bd",
                "ip": "10.10.50.77",
                "hostname": "camera",
                "signal": -84,
                "vlan_id": 2,
            }],
        }],
    }
    driver = UnifiProtocol({"ip": "10.20.1.5", "username": "ubnt"})
    driver.execute_command = Mock(return_value=json.dumps(dump))

    data = driver.scrape()

    assert data["name"] == "LandingAP"
    assert data["model"] == "UAP-AC-LR"
    assert data["hardware_model"] == "U7LR"
    assert data["role"] == "access_point"
    assert [port["port"] for port in data["ports"]] == ["1", "wifi1ap8"]
    assert data["ports"][0]["speed"] == "100M"
    assert data["ports"][1]["name"] == "Office-IOT (5 GHz)"
    assert data["ports"][1]["client_count"] == 1
    assert data["mac_table"] == [{
        "mac": "BC:FD:0C:B6:6E:BD",
        "port": "wifi1ap8",
        "vlan": "2",
        "type": "dynamic",
        "ip": "10.10.50.77",
        "hostname": "camera",
        "signal": -84,
        "ssid": "Office-IOT",
    }]
    assert data["neighbors"][0]["local_port"] == "1"
    assert data["neighbors"][0]["remote_chassis_id"] == "10.10.50.124"
    assert data["neighbors"][0]["remote_port_id"] == "A4:93:4C:FE:F8:C0"
    assert driver.execute_command.call_count == 1


def test_unifi_parses_linux_bridge_and_brctl_mac_tables():
    bridge_output = "10:20:30:40:50:60 dev eth1 master br0 vlan 20 dynamic\n"
    brctl_output = "port no mac addr is local? ageing timer\n2  AA:BB:CC:DD:EE:FF no  12.34\n"

    assert UnifiProtocol._parse_mac_table_text(bridge_output) == [{
        "mac": "10:20:30:40:50:60",
        "port": "eth1",
        "vlan": "20",
        "type": "dynamic",
    }]
    assert UnifiProtocol._parse_mac_table_text(brctl_output) == [{
        "mac": "AA:BB:CC:DD:EE:FF",
        "port": "2",
        "vlan": "1",
        "type": "dynamic",
    }]


def test_unifi_model_prefix_selection_and_ovs_ssh_inheritance():
    assert isinstance(
        ProtocolRegistry.create({"ip": "192.0.2.22", "model": "USW-Pro-24"}),
        UnifiProtocol,
    )
    assert issubclass(OVSProtocol, SSHProtocol)
    assert _infer_protocol({"model": "USW-Pro-24"}) == "unifi"
    assert _infer_protocol({"model": "generic_ssh"}) == "ssh"
