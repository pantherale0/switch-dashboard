import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from switch_dashboard.protocols.base import Capability
from switch_dashboard.protocols.drivers.proxmox import ProxmoxProtocol
from switch_dashboard.protocols.registry import ProtocolRegistry
from switch_dashboard.services.poller_service import PollerService


def _driver(**overrides):
    config = {
        "id": "cluster-main",
        "name": "Lab Cluster",
        "ip": "192.0.2.50",
        "api_user": "dashboard@pve",
        "token_id": "switch-dashboard",
        "token_secret": "secret-value",
    }
    config.update(overrides)
    return ProxmoxProtocol(config)


def test_proxmox_driver_registration_and_schema():
    protocols = {item["name"]: item for item in ProtocolRegistry.list_protocols()}
    assert "proxmox" in protocols
    assert Capability.CHILD_DEVICES.value in protocols["proxmox"]["capabilities"]
    fields = {field["name"]: field for field in protocols["proxmox"]["config_schema"]}
    assert fields["token_secret"]["type"] == "password"
    assert fields["verify_ssl"]["type"] == "boolean"

    valid, error, config = ProtocolRegistry.validate_config("proxmox", {
        "api_user": "dashboard@pve",
        "token_id": "switch-dashboard",
        "token_secret": "secret",
        "api_port": "8006",
    })
    assert valid is True
    assert error is None
    assert config["api_port"] == 8006
    assert isinstance(ProtocolRegistry.create({"ip": "192.0.2.50", "model": "pve", **config}), ProxmoxProtocol)


def test_proxmox_token_header_and_interface_parsing():
    driver = _driver()
    assert driver._headers() == {
        "Authorization": "PVEAPIToken=dashboard@pve!switch-dashboard=secret-value"
    }

    qemu = driver._parse_interfaces({
        "net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,firewall=1,tag=20",
        "net1": "e1000=AA:BB:CC:DD:EE:02,bridge=vmbr1",
    }, "qemu")
    assert qemu[0] == {
        "name": "net0",
        "config_key": "net0",
        "mac": "AA:BB:CC:DD:EE:01",
        "bridge": "vmbr0",
        "vlan": "20",
        "firewall": True,
    }

    lxc = driver._parse_interfaces({
        "net0": "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:03,ip=10.0.0.5/24,tag=30,type=veth",
    }, "lxc")
    assert lxc[0]["name"] == "eth0"
    assert lxc[0]["ip"] == "10.0.0.5/24"
    assert lxc[0]["vlan"] == "30"

    # Non-standard QEMU model keys should still yield the MAC.
    custom = driver._parse_interfaces({
        "net0": "igb=AA:BB:CC:DD:EE:09,bridge=vmbr0,tag=30",
    }, "qemu")
    assert custom[0]["mac"] == "AA:BB:CC:DD:EE:09"


def test_proxmox_cluster_scrape_normalizes_qemu_and_lxc_children():
    driver = _driver(verify_ssl=False)
    session_context = MagicMock()
    session = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=None)
    driver._session = MagicMock(return_value=session_context)

    resources = [
        {"vmid": 101, "name": "database", "type": "qemu", "node": "pve-01", "status": "running", "mem": 1024, "maxmem": 2048},
        {"vmid": 201, "name": "dns", "type": "lxc", "node": "pve-02", "status": "stopped"},
    ]

    async def request(_session, path, params=None):
        responses = {
            "version": {"release": "8.4"},
            "cluster/status": [
                {"type": "cluster", "name": "production"},
                {"type": "node", "name": "pve-01", "ip": "192.0.2.50", "local": 1},
                {"type": "node", "name": "pve-02", "ip": "192.0.2.51"},
            ],
            "nodes": [
                {"node": "pve-01", "status": "online", "uptime": 500},
                {"node": "pve-02", "status": "online", "uptime": 400},
            ],
            "cluster/resources": resources,
            "nodes/pve-01/network": [
                {"iface": "vmbr0", "type": "bridge", "hwaddress": "00:11:22:33:44:99", "address": "192.0.2.50", "cidr": "192.0.2.50/24", "bridge_ports": "eno1", "active": 1},
                {"iface": "eno1", "type": "eth", "hwaddress": "00:11:22:33:44:01", "active": 1},
            ],
            "nodes/pve-02/network": [
                {"iface": "eno1", "type": "eth", "mac": "00:11:22:33:44:02", "active": 1},
                {"iface": "bond0", "type": "bond", "hwaddr": "00:11:22:33:44:03", "active": 1},
            ],
            "nodes/pve-01/qemu/101/config": {"net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0,tag=20"},
            "nodes/pve-02/lxc/201/config": {"net0": "name=eth0,hwaddr=AA:BB:CC:DD:EE:02,bridge=vmbr1,ip=dhcp"},
        }
        return responses[path]

    driver._request_json = AsyncMock(side_effect=request)
    data = asyncio.run(driver._scrape_async())

    assert data["status"] == "online"
    assert data["cluster_name"] == "production"
    assert data["node_count"] == 2
    assert data["guest_count"] == 2
    assert data["ports"] == []
    assert data["role"] == "virtualisation_host"
    assert data["topology_hub"] is True
    assert data["cluster_nodes"][0]["local"] is True
    assert data["cluster_nodes"][0]["ip"] == "192.0.2.50"
    assert data["cluster_nodes"][0]["mac"] == "00:11:22:33:44:01"
    assert data["cluster_nodes"][0]["macs"] == ["00:11:22:33:44:01", "00:11:22:33:44:99"]
    assert {item["name"] for item in data["cluster_nodes"][0]["interfaces"]} == {"eno1", "vmbr0"}
    assert data["cluster_nodes"][1]["mac"] in {"00:11:22:33:44:02", "00:11:22:33:44:03"}
    assert {"00:11:22:33:44:02", "00:11:22:33:44:03"}.issubset(set(data["cluster_nodes"][1]["macs"]))

    vm, container = data["child_devices"]
    assert vm["id"] == "proxmox:cluster-main:qemu:101"
    assert vm["kind"] == "virtual_machine"
    assert vm["status"] == "online"
    assert vm["macs"] == ["AA:BB:CC:DD:EE:01"]
    assert container["id"] == "proxmox:cluster-main:lxc:201"
    assert container["kind"] == "lxc_container"
    assert container["status"] == "stopped"
    assert container["ips"] == []


def test_proxmox_partial_guest_config_failure_preserves_inventory():
    driver = _driver()
    session = MagicMock()
    resources = [{"vmid": 300, "name": "unavailable-config", "type": "qemu", "node": "pve-01", "status": "running"}]
    driver._request_json = AsyncMock(side_effect=RuntimeError("permission denied"))

    children = asyncio.run(driver._load_children(session, resources))
    assert len(children) == 1
    assert children[0]["name"] == "unavailable-config"
    assert children[0]["status"] == "online"
    assert "permission denied" in children[0]["config_error"]


def test_proxmox_scrape_failure_returns_portless_offline_host():
    driver = _driver()
    driver._scrape_async = AsyncMock(side_effect=RuntimeError("API unavailable"))

    data = driver.scrape()
    assert data["status"] == "offline"
    assert data["ports"] == []
    assert data["child_devices"] == []
    assert "API unavailable" in data["error"]


def test_poller_preserves_children_and_skips_mac_scrape_without_capability():
    metric_repo = MagicMock()
    device_repo = MagicMock()
    poller = PollerService(metric_repo=metric_repo, device_repo=device_repo)
    protocol = MagicMock()
    protocol.protocol_name = "proxmox"
    protocol.scrape.return_value = {
        "name": "Lab Cluster",
        "ip": "192.0.2.50",
        "status": "online",
        "ports": [],
        "child_devices": [{"id": "proxmox:lab:qemu:100", "kind": "virtual_machine"}],
    }
    protocol.has_capability.side_effect = lambda capability: capability == Capability.CHILD_DEVICES

    with patch.object(ProtocolRegistry, "create", return_value=protocol):
        _, data, _, _, _ = poller._poll_single_switch(
            {
                "ip": "192.0.2.50",
                "name": "Lab Cluster",
                "role": "virtualisation_host",
                "device_type": "virtualisation_host",
                "protocol": "proxmox",
            },
            counters={},
            now=1000.0,
            settings={"refresh_interval": 30, "mac_refresh_multiplier": 5},
        )

    assert data["status"] == "online"
    assert data["child_devices"][0]["id"] == "proxmox:lab:qemu:100"
    protocol.scrape_mac_table.assert_not_called()
    device_repo.update_mac_table.assert_not_called()


def test_proxmox_altnames_mac_extraction():
    driver = _driver()
    iface1 = {
        "iface": "msfp0",
        "type": "eth",
        "altnames": ["enx0002c910ee64"],
        "active": 1,
    }
    assert driver._extract_interface_mac(iface1) == "00:02:C9:10:EE:64"

    iface2 = {
        "iface": "eno1",
        "type": "eth",
        "altnames": ["enp0s31f6", "enxc8d9d204f94e"],
        "active": 1,
    }
    assert driver._extract_interface_mac(iface2) == "C8:D9:D2:04:F9:4E"

    iface3 = {
        "iface": "enxe0d4e80b5e15",
        "type": "eth",
        "active": 1,
    }
    assert driver._extract_interface_mac(iface3) == "E0:D4:E8:0B:5E:15"
