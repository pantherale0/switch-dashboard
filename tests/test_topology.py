import unittest
from unittest.mock import MagicMock

from switch_dashboard.services.topology_service import TopologyService, parse_speed_bps
from switch_dashboard.core.models import NodeRole, PortRole


class TestTopologyService(unittest.TestCase):
    def test_parse_speed_bps(self):
        self.assertEqual(parse_speed_bps("10G"), 10_000_000_000)
        self.assertEqual(parse_speed_bps("2.5G"), 2_500_000_000)
        self.assertEqual(parse_speed_bps("1G"), 1_000_000_000)
        self.assertEqual(parse_speed_bps("1000M"), 1_000_000_000)
        self.assertEqual(parse_speed_bps("100M"), 100_000_000)
        self.assertEqual(parse_speed_bps("1G/300M"), 1_000_000_000)
        self.assertEqual(parse_speed_bps(""), 1_000_000_000)

    def test_multi_switch_chain_resolution(self):
        """Tests that a 4-switch chain (Router -> Dist -> Core -> Access) resolves without phantom shortcuts."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = "TestVendor"

        # Mock polled data for 4 switches
        router_ip = "192.168.1.1"
        dist_ip = "192.168.1.2"
        core_ip = "192.168.1.3"
        access_ip = "192.168.1.4"

        router_mac = "AA0000000001"
        dist_mac = "AA0000000002"
        core_mac = "AA0000000003"
        access_mac = "AA0000000004"
        client1_mac = "DD0000000001"

        cached_data = {
            router_ip: {
                "mac": router_mac,
                "ports": [
                    {"port": 1, "link": "down", "speed": "1G"},
                    {"port": 2, "link": "up", "speed": "10G", "speed_tx_bps": 100_000_000, "speed_rx_bps": 50_000_000},
                ],
                "mac_table": [
                    {"port": 2, "mac": dist_mac},
                    {"port": 2, "mac": core_mac},
                    {"port": 2, "mac": access_mac},
                    {"port": 2, "mac": client1_mac},
                ],
            },
            dist_ip: {
                "mac": dist_mac,
                "ports": [
                    {"port": 1, "link": "up", "speed": "10G"},
                    {"port": 24, "link": "up", "speed": "10G"},
                ],
                "mac_table": [
                    {"port": 1, "mac": router_mac},
                    {"port": 24, "mac": core_mac},
                    {"port": 24, "mac": access_mac},
                    {"port": 24, "mac": client1_mac},
                ],
            },
            core_ip: {
                "mac": core_mac,
                "ports": [
                    {"port": 1, "link": "up", "speed": "10G"},
                    {"port": 12, "link": "up", "speed": "2.5G"},
                ],
                "mac_table": [
                    {"port": 1, "mac": router_mac},
                    {"port": 1, "mac": dist_mac},
                    {"port": 12, "mac": access_mac},
                    {"port": 12, "mac": client1_mac},
                ],
            },
            access_ip: {
                "mac": access_mac,
                "ports": [
                    {"port": 1, "link": "up", "speed": "2.5G"},
                    {"port": 3, "link": "up", "speed": "1G"},
                ],
                "mac_table": [
                    {"port": 1, "mac": router_mac},
                    {"port": 1, "mac": dist_mac},
                    {"port": 1, "mac": core_mac},
                    {"port": 3, "mac": client1_mac},
                ],
            },
        }

        mock_poller.get_cached_data.return_value = cached_data

        topo_service = TopologyService(poller_service=mock_poller, vendor_service=mock_vendor)

        # Mock config with the 4 switches
        test_config = {
            "switches": [
                {"ip": router_ip, "name": "Core Router", "model": "generic_router", "mac": router_mac, "role": "router"},
                {"ip": dist_ip, "name": "Distribution Switch", "model": "dist_switch", "mac": dist_mac, "role": "distribution"},
                {"ip": core_ip, "name": "Core Switch", "model": "core_switch", "mac": core_mac, "role": "core"},
                {"ip": access_ip, "name": "Access Switch", "model": "access_switch", "mac": access_mac, "role": "access"},
            ],
            "clients": {
                client1_mac: {"host": "Workstation-1", "mac": "DD:00:00:00:00:01"},
            },
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes = {n["id"]: n for n in result["nodes"]}
        links = result["links"]

        # Verify all switches and nodes exist
        self.assertIn(router_ip, nodes)
        self.assertIn(dist_ip, nodes)
        self.assertIn(core_ip, nodes)
        self.assertIn(access_ip, nodes)
        self.assertIn(client1_mac, nodes)

        # Verify direct link pairs
        link_pairs = set()
        for l in links:
            link_pairs.add(tuple(sorted([l["source"], l["target"]])))

        # Expect Router <-> Dist
        self.assertIn(tuple(sorted([router_ip, dist_ip])), link_pairs)
        # Expect Dist <-> Core
        self.assertIn(tuple(sorted([dist_ip, core_ip])), link_pairs)
        # Expect Core <-> Access
        self.assertIn(tuple(sorted([core_ip, access_ip])), link_pairs)

        # MUST NOT have phantom shortcuts bypassing intermediate switches
        self.assertNotIn(tuple(sorted([router_ip, core_ip])), link_pairs)
        self.assertNotIn(tuple(sorted([router_ip, access_ip])), link_pairs)
        self.assertNotIn(tuple(sorted([dist_ip, access_ip])), link_pairs)

        # Verify Client1 is attached exclusively to AccessSwitch
        client1_links = [l for l in links if l["target"] == client1_mac or l["source"] == client1_mac]
        self.assertEqual(len(client1_links), 1)
        self.assertEqual(client1_links[0]["source"], access_ip)

    def test_client_on_intermediate_distribution_switch(self):
        """Tests that a client connected to an intermediate distribution switch is correctly attributed."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = "NASVendor"

        router_ip = "192.168.1.1"
        dist_ip = "192.168.1.2"
        nas_mac = "BB0000000001"

        cached_data = {
            router_ip: {
                "mac": "AA0000000001",
                "ports": [{"port": 1, "link": "up", "speed": "10G"}],
                "mac_table": [
                    {"port": 1, "mac": "AA0000000002"},
                    {"port": 1, "mac": nas_mac},
                ],
            },
            dist_ip: {
                "mac": "AA0000000002",
                "ports": [
                    {"port": 1, "link": "up", "speed": "10G"},
                    {"port": 5, "link": "up", "speed": "1G"},
                ],
                "mac_table": [
                    {"port": 1, "mac": "AA0000000001"},
                    {"port": 5, "mac": nas_mac},
                ],
            },
        }
        mock_poller.get_cached_data.return_value = cached_data

        topo_service = TopologyService(poller_service=mock_poller, vendor_service=mock_vendor)
        test_config = {
            "switches": [
                {"ip": router_ip, "name": "Router", "mac": "AA0000000001", "role": "router"},
                {"ip": dist_ip, "name": "DistSwitch", "mac": "AA0000000002", "role": "distribution"},
            ],
            "clients": {
                nas_mac: {"host": "Storage-NAS", "mac": "BB:00:00:00:00:01"},
            },
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nas_links = [l for l in result["links"] if l["target"] == nas_mac]
        self.assertEqual(len(nas_links), 1)
        self.assertEqual(nas_links[0]["source"], dist_ip)
        self.assertEqual(nas_links[0]["source_port"], "Port 5")

    def test_access_point_and_wifi_client_bridging(self):
        """Tests that Access Points are identified and downstream Wi-Fi clients are bridged to the AP."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = "Ubiquiti"

        dist_ip = "192.168.1.2"
        ap_mac = "CC0000000001"
        phone_mac = "EE0000000001"
        laptop_mac = "EE0000000002"

        cached_data = {
            dist_ip: {
                "mac": "AA0000000002",
                "ports": [
                    {"port": 8, "link": "up", "speed": "1G", "speed_tx_bps": 25_000_000, "speed_rx_bps": 10_000_000},
                ],
                "mac_table": [
                    {"port": 8, "mac": ap_mac},
                    {"port": 8, "mac": phone_mac},
                    {"port": 8, "mac": laptop_mac},
                ],
            },
        }
        mock_poller.get_cached_data.return_value = cached_data

        topo_service = TopologyService(poller_service=mock_poller, vendor_service=mock_vendor)
        test_config = {
            "switches": [
                {"ip": dist_ip, "name": "DistSwitch", "mac": "AA0000000002", "role": "distribution"},
            ],
            "access_points": [
                {"mac": ap_mac, "name": "AP-Hallway", "model": "U6-Pro", "ip": "192.168.1.50"},
            ],
            "clients": {
                phone_mac: {"host": "iPhone-Jordan", "mac": "EE:00:00:00:00:01"},
                laptop_mac: {"host": "MacBook-Air", "mac": "EE:00:00:00:00:02"},
            },
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes = {n["id"]: n for n in result["nodes"]}
        ap_id = f"ap_{ap_mac}"
        self.assertIn(ap_id, nodes)
        self.assertEqual(nodes[ap_id]["name"], "AP-Hallway")
        self.assertEqual(nodes[ap_id]["type"], NodeRole.ACCESS_POINT)

        # DistSwitch to AP link
        ap_links = [l for l in result["links"] if l["source"] == dist_ip and l["target"] == ap_id]
        self.assertEqual(len(ap_links), 1)
        self.assertEqual(ap_links[0]["type"], "ap")
        self.assertEqual(ap_links[0]["source_port"], "Port 8")

        # Wi-Fi clients attached to AP
        phone_links = [l for l in result["links"] if l["target"] == phone_mac]
        self.assertEqual(len(phone_links), 1)
        self.assertEqual(phone_links[0]["source"], ap_id)
        self.assertEqual(phone_links[0]["type"], "client")

        laptop_links = [l for l in result["links"] if l["target"] == laptop_mac]
        self.assertEqual(len(laptop_links), 1)
        self.assertEqual(laptop_links[0]["source"], ap_id)

    def test_traffic_telemetry_and_utilization(self):
        """Tests that link utilization percentage and capacity bps are computed accurately."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""

        sw1_ip = "192.168.1.1"
        sw2_ip = "192.168.1.2"

        cached_data = {
            sw1_ip: {
                "mac": "AA0000000001",
                "ports": [
                    {"port": 1, "link": "up", "speed": "10G", "speed_tx_bps": 8_500_000_000, "speed_rx_bps": 2_000_000_000},
                ],
                "mac_table": [{"port": 1, "mac": "AA0000000002"}],
            },
            sw2_ip: {
                "mac": "AA0000000002",
                "ports": [
                    {"port": 1, "link": "up", "speed": "10G"},
                ],
                "mac_table": [{"port": 1, "mac": "AA0000000001"}],
            },
        }
        mock_poller.get_cached_data.return_value = cached_data

        topo_service = TopologyService(poller_service=mock_poller, vendor_service=mock_vendor)
        test_config = {
            "switches": [
                {"ip": sw1_ip, "name": "Switch1", "mac": "AA0000000001"},
                {"ip": sw2_ip, "name": "Switch2", "mac": "AA0000000002"},
            ],
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        link = next(l for l in result["links"] if {l["source"], l["target"]} == {sw1_ip, sw2_ip})
        self.assertEqual(link["capacity_bps"], 10_000_000_000)
        self.assertEqual(max(link["tx_bps"], link["rx_bps"]), 8_500_000_000)
        self.assertEqual(min(link["tx_bps"], link["rx_bps"]), 2_000_000_000)
        self.assertEqual(link["utilization_pct"], 85.0)

    def test_lldp_neighbor_peering(self):
        """Tests that LLDP neighbor reports establish high-confidence direct links."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""

        sw1_ip = "192.168.1.1"
        sw2_ip = "192.168.1.2"

        cached_data = {
            sw1_ip: {
                "mac": "AA0000000001",
                "ports": [{"port": "1", "link": "up", "speed": "1G"}],
                "mac_table": [],
                "neighbors": [
                    {
                        "local_port": "1",
                        "remote_chassis_id": "AA:00:00:00:00:02",
                        "remote_port_id": "24",
                        "remote_system_name": "Switch2",
                    }
                ],
            },
            sw2_ip: {
                "mac": "AA0000000002",
                "ports": [{"port": "24", "link": "up", "speed": "1G"}],
                "mac_table": [],
                "neighbors": [],
            },
        }
        mock_poller.get_cached_data.return_value = cached_data

        topo_service = TopologyService(poller_service=mock_poller, vendor_service=mock_vendor)
        test_config = {
            "switches": [
                {"ip": sw1_ip, "name": "Switch1", "mac": "AA0000000001"},
                {"ip": sw2_ip, "name": "Switch2", "mac": "AA0000000002"},
            ],
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        link_pairs = {tuple(sorted([l["source"], l["target"]])) for l in result["links"]}
        self.assertIn(tuple(sorted([sw1_ip, sw2_ip])), link_pairs)

    def test_virtualisation_host_and_proxmox_guest_children(self):
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""
        mock_repo = MagicMock()
        mock_repo.get_mac_table.return_value = []

        router_ip = "192.168.1.1"
        host_ip = "192.168.1.20"
        physical_host_id = "proxmox:cluster-main:node:pve-01"
        guest_mac = "AABBCCDDEE01"
        guest_id = "proxmox:cluster-main:qemu:101"
        mock_poller.get_cached_data.return_value = {
            router_ip: {
                "status": "online",
                "mac": "AA0000000001",
                "ports": [{"port": 1, "link": "up", "speed": "10G"}],
                "mac_table": [{"port": 1, "mac": guest_mac}],
            },
            host_ip: {
                "status": "online",
                "ports": [],
                "mac_table": [],
                "cluster_name": "production",
                "cluster_nodes": [{"id": physical_host_id, "name": "pve-01", "status": "online"}],
                "child_devices": [{
                    "id": guest_id,
                    "native_id": "101",
                    "name": "database",
                    "kind": "virtual_machine",
                    "status": "online",
                    "state": "running",
                    "node": "pve-01",
                    "macs": ["AA:BB:CC:DD:EE:01"],
                    "ips": ["10.0.0.10"],
                    "interfaces": [{"name": "net0", "mac": "AA:BB:CC:DD:EE:01", "bridge": "vmbr0", "vlan": "20"}],
                }],
            },
        }

        topo_service = TopologyService(
            poller_service=mock_poller,
            vendor_service=mock_vendor,
            device_repo=mock_repo,
        )
        test_config = {
            "switches": [
                {"ip": router_ip, "name": "Router", "mac": "AA0000000001", "role": "router"},
                {
                    "id": "cluster-main",
                    "ip": host_ip,
                    "name": "Proxmox Cluster",
                    "model": "proxmox",
                    "protocol": "proxmox",
                    "role": "other",
                    "device_type": "other",
                    "parent_ip": router_ip,
                    "parent_port": "1",
                },
            ],
            "clients": {
                guest_mac: {"host": "duplicate-database", "mac": "AA:BB:CC:DD:EE:01"},
            },
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertNotIn(host_ip, nodes)
        self.assertEqual(nodes[physical_host_id]["type"], NodeRole.VIRTUALISATION_HOST)
        self.assertEqual(nodes[physical_host_id]["child_count"], 1)
        self.assertEqual(nodes[guest_id]["type"], NodeRole.VIRTUAL_MACHINE)
        self.assertEqual(nodes[guest_id]["parent_host_id"], physical_host_id)
        self.assertNotIn(guest_mac, nodes)

        guest_links = [link for link in result["links"] if link.get("type") == "virtual_child"]
        self.assertEqual(len(guest_links), 1)
        self.assertEqual(guest_links[0]["source"], physical_host_id)
        self.assertEqual(guest_links[0]["target"], guest_id)
        self.assertFalse(any(host_ip in {link["source"], link["target"]} for link in result["links"]))

    def test_proxmox_physical_hosts_link_by_mac_and_own_their_guests(self):
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""
        mock_repo = MagicMock()
        mock_repo.get_mac_table.return_value = []

        switch_ip = "192.168.1.2"
        endpoint_ip = "192.168.1.20"
        local_host_id = "proxmox:cluster-main:node:pve-01"
        remote_host_id = "proxmox:cluster-main:node:pve-02"
        local_mac = "001122334401"
        remote_mac = "001122334402"
        local_guest_id = "proxmox:cluster-main:qemu:101"
        remote_guest_id = "proxmox:cluster-main:lxc:201"
        mock_poller.get_cached_data.return_value = {
            switch_ip: {
                "status": "online",
                "mac": "AA0000000002",
                "ports": [
                    {"port": 5, "link": "up", "speed": "10G"},
                    {"port": 6, "link": "up", "speed": "10G"},
                ],
                "mac_table": [
                    {"port": 5, "mac": local_mac},
                    {"port": 6, "mac": remote_mac},
                ],
            },
            endpoint_ip: {
                "status": "online",
                "ports": [],
                "mac_table": [],
                "cluster_name": "production",
                "cluster_nodes": [
                    {
                        "id": local_host_id,
                        "name": "pve-01",
                        "status": "online",
                        "local": True,
                        "ip": endpoint_ip,
                        "ips": [endpoint_ip],
                        "mac": "00:11:22:33:44:01",
                        "macs": ["00:11:22:33:44:01"],
                        "interfaces": [{"name": "eno1", "mac": "00:11:22:33:44:01"}],
                    },
                    {
                        "id": remote_host_id,
                        "name": "pve-02",
                        "status": "online",
                        "local": False,
                        "ip": "192.168.1.21",
                        "ips": ["192.168.1.21"],
                        "mac": "",
                        "macs": [],
                        "interfaces": [{"name": "eno1", "mac": ""}],
                    },
                ],
                "child_devices": [
                    {
                        "id": local_guest_id,
                        "native_id": "101",
                        "name": "database",
                        "kind": "virtual_machine",
                        "status": "online",
                        "node": "PVE-01.LOCALDOMAIN",
                        "macs": ["AA:BB:CC:DD:EE:01"],
                    },
                    {
                        "id": remote_guest_id,
                        "native_id": "201",
                        "name": "dns",
                        "kind": "lxc_container",
                        "status": "online",
                        "node": "PVE-02",
                        "macs": ["AA:BB:CC:DD:EE:02"],
                    },
                ],
            },
        }
        topo_service = TopologyService(
            poller_service=mock_poller,
            vendor_service=mock_vendor,
            device_repo=mock_repo,
        )
        test_config = {
            "switches": [
                {"ip": switch_ip, "name": "Core Switch", "role": "core", "mac": "AA0000000002"},
                {
                    "id": "cluster-main",
                    "ip": endpoint_ip,
                    "name": "Proxmox Cluster",
                    "model": "proxmox",
                    "protocol": "proxmox",
                    "role": "virtualisation_host",
                },
            ],
            "clients": {
                local_mac: {"host": "pve-01", "mac": "00:11:22:33:44:01", "ip": endpoint_ip},
                remote_mac: {"host": "pve-02", "mac": "00:11:22:33:44:02", "scanner_ip": "192.168.1.21"},
            },
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertNotIn(endpoint_ip, nodes)
        self.assertEqual(len(result["hubs"]), 1)
        self.assertEqual(result["hubs"][0]["id"], "cluster-main")
        self.assertEqual(
            set(result["hubs"][0]["node_ids"]),
            {local_host_id, remote_host_id, local_guest_id, remote_guest_id},
        )
        self.assertEqual(nodes[local_host_id]["name"], "pve-01")
        self.assertEqual(nodes[local_host_id]["mac"], local_mac)
        self.assertEqual(nodes[local_host_id]["child_count"], 1)
        self.assertEqual(nodes[remote_host_id]["name"], "pve-02")
        self.assertEqual(nodes[remote_host_id]["mac"], remote_mac)
        self.assertEqual(nodes[remote_host_id]["child_count"], 1)
        self.assertEqual(nodes[remote_host_id]["topology_hub_id"], "cluster-main")
        self.assertNotIn(local_mac, {node["id"] for node in result["nodes"]})
        self.assertNotIn(remote_mac, {node["id"] for node in result["nodes"]})
        self.assertEqual(nodes[local_guest_id]["parent_host_id"], local_host_id)
        self.assertEqual(nodes[remote_guest_id]["parent_host_id"], remote_host_id)
        self.assertEqual(nodes[remote_guest_id]["topology_hub_id"], "cluster-main")

        physical_links = [link for link in result["links"] if link.get("type") == "physical_host"]
        self.assertEqual({link["target"] for link in physical_links}, {local_host_id, remote_host_id})
        self.assertEqual(
            {link["source_port"] for link in physical_links}, {"Port 5", "Port 6"}
        )
        guest_links = [link for link in result["links"] if link.get("type") == "virtual_child"]
        self.assertIn(
            (remote_host_id, remote_guest_id),
            {(link["source"], link["target"]) for link in guest_links},
        )

    def test_proxmox_guest_fingerprint_suppresses_client_duplicates_without_mac(self):
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""
        mock_repo = MagicMock()
        mock_repo.get_mac_table.return_value = []

        switch_ip = "192.168.1.2"
        endpoint_ip = "192.168.1.20"
        host_id = "proxmox:cluster-main:node:pve-01"
        guest_id = "proxmox:cluster-main:qemu:101"
        mock_poller.get_cached_data.return_value = {
            switch_ip: {
                "status": "online",
                "mac": "AA0000000002",
                "ports": [{"port": 5, "link": "up", "speed": "10G"}],
                "mac_table": [],
            },
            endpoint_ip: {
                "status": "online",
                "ports": [],
                "mac_table": [],
                "cluster_name": "production",
                "cluster_nodes": [{
                    "id": host_id,
                    "name": "pve-01",
                    "status": "online",
                    "local": True,
                    "ip": endpoint_ip,
                    "ips": [endpoint_ip],
                    "mac": "00:11:22:33:44:01",
                    "macs": ["00:11:22:33:44:01"],
                    "interfaces": [{"name": "eno1", "mac": "00:11:22:33:44:01"}],
                }],
                "child_devices": [{
                    "id": guest_id,
                    "native_id": "101",
                    "name": "hyperion",
                    "kind": "virtual_machine",
                    "status": "online",
                    "node": "pve-01",
                    "macs": [],
                    "ips": ["192.168.1.70"],
                }],
            },
        }
        topo_service = TopologyService(
            poller_service=mock_poller,
            vendor_service=mock_vendor,
            device_repo=mock_repo,
        )
        test_config = {
            "switches": [
                {"ip": switch_ip, "name": "Core Switch", "role": "core", "mac": "AA0000000002"},
                {
                    "id": "cluster-main",
                    "ip": endpoint_ip,
                    "name": "Proxmox Cluster",
                    "model": "proxmox",
                    "protocol": "proxmox",
                    "role": "virtualisation_host",
                },
            ],
            "clients": {
                "AABBCCDDEE01": {"host": "hyperion", "ip": "192.168.1.70", "mac": "AA:BB:CC:DD:EE:01"},
            },
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertIn(guest_id, nodes)
        self.assertNotIn("AABBCCDDEE01", nodes)

    def test_physical_proxmox_host_prefers_non_router_attachment(self):
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""
        mock_repo = MagicMock()
        mock_repo.get_mac_table.return_value = []

        router_ip = "192.168.1.1"
        core_ip = "192.168.1.2"
        endpoint_ip = "10.20.1.90"
        host_id = "proxmox:cluster-main:node:pve-01"
        host_mac = "001122334401"

        mock_poller.get_cached_data.return_value = {
            router_ip: {
                "status": "online",
                "mac": "AA0000000001",
                "ports": [{"port": 7, "link": "up", "speed": "1G"}],
                "mac_table": [
                    {"port": 7, "mac": "AA0000000002"},
                    {"port": 7, "mac": host_mac},
                ],
            },
            core_ip: {
                "status": "online",
                "mac": "AA0000000002",
                "ports": [
                    {"port": 1, "link": "up", "speed": "1G"},
                    {"port": 5, "link": "up", "speed": "1G"},
                ],
                "mac_table": [
                    {"port": 1, "mac": "AA0000000001"},
                    {"port": 5, "mac": host_mac},
                ],
            },
            endpoint_ip: {
                "status": "online",
                "ports": [],
                "mac_table": [],
                "cluster_name": "production",
                "cluster_nodes": [{
                    "id": host_id,
                    "name": "pve-01",
                    "status": "online",
                    "local": True,
                    "ip": endpoint_ip,
                    "ips": [endpoint_ip],
                    "mac": "00:11:22:33:44:01",
                    "macs": ["00:11:22:33:44:01"],
                    "interfaces": [{"name": "eno1", "mac": "00:11:22:33:44:01"}],
                }],
                "child_devices": [],
            },
        }

        topo_service = TopologyService(
            poller_service=mock_poller,
            vendor_service=mock_vendor,
            device_repo=mock_repo,
        )
        test_config = {
            "switches": [
                {"ip": router_ip, "name": "Router", "role": "router", "mac": "AA0000000001"},
                {"ip": core_ip, "name": "Core Switch", "role": "core", "mac": "AA0000000002"},
                {
                    "id": "cluster-main",
                    "ip": endpoint_ip,
                    "name": "ATTPRX",
                    "model": "proxmox",
                    "protocol": "proxmox",
                    "role": "other",
                    "device_type": "other",
                },
            ],
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        physical_links = [link for link in result["links"] if link.get("type") == "physical_host"]
        self.assertEqual(len(physical_links), 1)
        self.assertEqual(physical_links[0]["source"], core_ip)
        self.assertEqual(physical_links[0]["target"], host_id)

    def test_physical_proxmox_host_does_not_use_uplink_occupied_port(self):
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""
        mock_repo = MagicMock()
        mock_repo.get_mac_table.return_value = []

        dist_ip = "192.168.1.90"
        core_ip = "192.168.1.92"
        endpoint_ip = "10.20.1.90"
        host_id = "proxmox:cluster-main:node:pve-01"
        host_mac = "001122334401"

        mock_poller.get_cached_data.return_value = {
            dist_ip: {
                "status": "online",
                "mac": "AA0000000090",
                "ports": [
                    {"port": 5, "link": "up", "speed": "1G"},
                ],
                "mac_table": [
                    {"port": 5, "mac": "AA0000000092"},
                    {"port": 5, "mac": host_mac},
                ],
            },
            core_ip: {
                "status": "online",
                "mac": "AA0000000092",
                "ports": [
                    {"port": 8, "link": "up", "speed": "1G"},
                    {"port": 7, "link": "up", "speed": "1G"},
                ],
                "mac_table": [
                    {"port": 8, "mac": "AA0000000090"},
                    {"port": 7, "mac": host_mac},
                ],
            },
            endpoint_ip: {
                "status": "online",
                "ports": [],
                "mac_table": [],
                "cluster_name": "production",
                "cluster_nodes": [{
                    "id": host_id,
                    "name": "pve-01",
                    "status": "online",
                    "local": True,
                    "ip": endpoint_ip,
                    "ips": [endpoint_ip],
                    "mac": "00:11:22:33:44:01",
                    "macs": ["00:11:22:33:44:01"],
                    "interfaces": [{"name": "eno1", "mac": "00:11:22:33:44:01"}],
                }],
                "child_devices": [],
            },
        }

        topo_service = TopologyService(
            poller_service=mock_poller,
            vendor_service=mock_vendor,
            device_repo=mock_repo,
        )
        test_config = {
            "switches": [
                {
                    "ip": dist_ip,
                    "name": "Distribution Switch",
                    "role": "distribution",
                    "mac": "AA0000000090",
                },
                {
                    "ip": core_ip,
                    "name": "Core Switch",
                    "role": "core",
                    "mac": "AA0000000092",
                    "parent_ip": dist_ip,
                    "parent_port": 5,
                    "uplink_port": 8,
                },
                {
                    "id": "cluster-main",
                    "ip": endpoint_ip,
                    "name": "MSYSPHYSTOR",
                    "model": "proxmox",
                    "protocol": "proxmox",
                    "role": "other",
                    "device_type": "other",
                },
            ],
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        physical_links = [link for link in result["links"] if link.get("type") == "physical_host"]
        self.assertEqual(len(physical_links), 1)
        self.assertEqual(physical_links[0]["source"], core_ip)
        self.assertEqual(physical_links[0]["source_port"], "Port 7")
        self.assertEqual(physical_links[0]["target"], host_id)

    def test_physical_proxmox_host_ignores_router_vlan_candidate(self):
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""
        mock_repo = MagicMock()
        mock_repo.get_mac_table.return_value = []

        router_ip = "192.168.1.1"
        endpoint_ip = "10.20.1.90"
        host_id = "proxmox:cluster-main:node:pve-01"
        host_mac = "001122334401"

        mock_poller.get_cached_data.return_value = {
            router_ip: {
                "status": "online",
                "mac": "AA0000000001",
                "ports": [{"port": "VLAN1", "link": "up", "speed": "1G"}],
                "mac_table": [
                    {"port": "VLAN1", "mac": host_mac},
                ],
            },
            endpoint_ip: {
                "status": "online",
                "ports": [],
                "mac_table": [],
                "cluster_name": "production",
                "cluster_nodes": [{
                    "id": host_id,
                    "name": "pve-01",
                    "status": "online",
                    "local": True,
                    "ip": endpoint_ip,
                    "ips": [endpoint_ip],
                    "mac": "00:11:22:33:44:01",
                    "macs": ["00:11:22:33:44:01"],
                    "interfaces": [{"name": "eno1", "mac": "00:11:22:33:44:01"}],
                }],
                "child_devices": [],
            },
        }

        topo_service = TopologyService(
            poller_service=mock_poller,
            vendor_service=mock_vendor,
            device_repo=mock_repo,
        )
        test_config = {
            "switches": [
                {"ip": router_ip, "name": "Router", "role": "router", "mac": "AA0000000001"},
                {
                    "id": "cluster-main",
                    "ip": endpoint_ip,
                    "name": "MSYSPHYSTOR",
                    "model": "proxmox",
                    "protocol": "proxmox",
                    "role": "other",
                    "device_type": "other",
                },
            ],
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            result = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        physical_links = [link for link in result["links"] if link.get("type") == "physical_host"]
        self.assertEqual(physical_links, [])

    def test_physical_proxmox_host_keeps_last_good_non_router_link_on_transient_gap(self):
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""
        mock_repo = MagicMock()
        mock_repo.get_mac_table.return_value = []

        router_ip = "192.168.1.1"
        core_ip = "192.168.1.2"
        endpoint_ip = "10.20.1.90"
        host_id = "proxmox:cluster-main:node:pve-01"
        host_mac = "001122334401"

        common_host = {
            "status": "online",
            "ports": [],
            "mac_table": [],
            "cluster_name": "production",
            "cluster_nodes": [{
                "id": host_id,
                "name": "pve-01",
                "status": "online",
                "local": True,
                "ip": endpoint_ip,
                "ips": [endpoint_ip],
                "mac": "00:11:22:33:44:01",
                "macs": ["00:11:22:33:44:01"],
                "interfaces": [{"name": "eno1", "mac": "00:11:22:33:44:01"}],
            }],
            "child_devices": [],
        }

        snapshot_1 = {
            router_ip: {
                "status": "online",
                "mac": "AA0000000001",
                "ports": [{"port": "VLAN1", "link": "up", "speed": "1G"}],
                "mac_table": [{"port": "VLAN1", "mac": host_mac}],
            },
            core_ip: {
                "status": "online",
                "mac": "AA0000000002",
                "ports": [{"port": 7, "link": "up", "speed": "1G"}],
                "mac_table": [{"port": 7, "mac": host_mac}],
            },
            endpoint_ip: common_host,
        }
        snapshot_2 = {
            router_ip: snapshot_1[router_ip],
            core_ip: {
                "status": "online",
                "mac": "AA0000000002",
                "ports": [{"port": 7, "link": "up", "speed": "1G"}],
                "mac_table": [],
            },
            endpoint_ip: common_host,
        }
        mock_poller.get_cached_data.side_effect = [snapshot_1, snapshot_2]

        topo_service = TopologyService(
            poller_service=mock_poller,
            vendor_service=mock_vendor,
            device_repo=mock_repo,
        )
        test_config = {
            "switches": [
                {"ip": router_ip, "name": "Router", "role": "router", "mac": "AA0000000001"},
                {"ip": core_ip, "name": "Core Switch", "role": "core", "mac": "AA0000000002"},
                {
                    "id": "cluster-main",
                    "ip": endpoint_ip,
                    "name": "MSYSPHYSTOR",
                    "model": "proxmox",
                    "protocol": "proxmox",
                    "role": "other",
                    "device_type": "other",
                },
            ],
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            first = topo_service.build_topology()
            second = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        first_links = [link for link in first["links"] if link.get("type") == "physical_host"]
        second_links = [link for link in second["links"] if link.get("type") == "physical_host"]
        self.assertEqual(len(first_links), 1)
        self.assertEqual(first_links[0]["source"], core_ip)
        self.assertEqual(first_links[0]["source_port"], "Port 7")
        self.assertEqual(len(second_links), 1)
        self.assertEqual(second_links[0]["source"], core_ip)
        self.assertEqual(second_links[0]["source_port"], "Port 7")

    def test_phone_mini_switch_topology_resolution(self):
        """Tests that a Phone acting as a mini-switch is placed as a node,
        has a link from parent switch, routes downstream switches via PC passthrough port,
        and routes clients on that port through the Phone."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = "DeskPhoneVendor"
        mock_repo = MagicMock()
        mock_repo.get_clients.return_value = []
        mock_repo.get_mac_lookup.return_value = {}

        core_ip = "192.168.1.1"
        child_switch_ip = "192.168.1.2"
        phone_id = "phone-desk-01"
        phone_mac = "000413AABBCC"
        phone_mac_colon = "00:04:13:AA:BB:CC"
        pc_client_mac = "DD0000000001"

        # Core switch has port 3 connected to phone and PC client behind phone
        polled_data = {
            core_ip: {
                "status": "online",
                "mac": "AA0000000001",
                "ports": [
                    {"port": 3, "link": "up", "speed": "1G", "speed_tx_bps": 1000, "speed_rx_bps": 1000},
                ],
                "mac_table": [
                    {"port": 3, "mac": phone_mac},
                    {"port": 3, "mac": pc_client_mac},
                ],
            },
            child_switch_ip: {
                "status": "online",
                "mac": "AA0000000002",
                "ports": [
                    {"port": 1, "link": "up", "speed": "1G"},
                ],
                "mac_table": [],
            },
        }
        mock_poller.get_cached_data.return_value = polled_data

        topo_service = TopologyService(
            poller_service=mock_poller,
            vendor_service=mock_vendor,
            device_repo=mock_repo,
        )

        test_config = {
            "switches": [
                {"ip": core_ip, "name": "Core Switch", "role": "core", "mac": "AA0000000001"},
                {
                    "ip": child_switch_ip,
                    "name": "Sub Switch",
                    "role": "access",
                    "parent_ip": phone_id,
                    "uplink_port": 1,
                    "mac": "AA0000000002",
                },
            ],
            "unmanaged_switches": [
                {
                    "id": phone_id,
                    "name": "Desk Phone",
                    "role": "phone",
                    "device_type": "phone",
                    "parent_ip": core_ip,
                    "parent_port": 3,
                    "mac": phone_mac_colon,
                    "is_mini_switch": True,
                    "passthrough_port": "PC",
                }
            ],
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            topo = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes = {n["id"]: n for n in topo["nodes"]}
        self.assertIn(phone_id, nodes)
        self.assertEqual(nodes[phone_id]["type"], "phone")
        self.assertEqual(nodes[phone_id]["role"], "phone")
        self.assertTrue(nodes[phone_id].get("is_mini_switch"))

        # Check Parent Switch -> Phone link
        phone_uplink = next((l for l in topo["links"] if l["source"] == core_ip and l["target"] == phone_id), None)
        self.assertIsNotNone(phone_uplink)
        self.assertEqual(phone_uplink["source_port"], "Port 3")

        # Check Phone -> Child Switch link
        switch_uplink = next((l for l in topo["links"] if l["source"] == phone_id and l["target"] == child_switch_ip), None)
        self.assertIsNotNone(switch_uplink)
        self.assertEqual(switch_uplink["source_port"], "PC")
        self.assertEqual(switch_uplink["target_port"], "Port 1")

        # Check Client link: routed through Phone's PC port
        client_link = next((l for l in topo["links"] if l["target"] == pc_client_mac), None)
        self.assertIsNotNone(client_link)
        self.assertEqual(client_link["source"], phone_id)
        self.assertEqual(client_link["source_port"], "PC")

        # Verify Phone's own MAC is not duplicated as a client
        self.assertNotIn(phone_mac, nodes)

    def test_discovered_client_phone_passthrough_topology(self):
        """Tests that a discovered client phone with passthrough enabled dynamically
        discovers its parent switch/port from MAC table and places downstream PC behind it."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.side_effect = lambda m, v, i: "Cisco Systems, Inc" if "A493" in m else "Microsoft Corporation"
        mock_repo = MagicMock()

        phone_mac_clean = "A4934CFEF8C0"
        phone_mac_colon = "A4:93:4C:FE:F8:C0"
        pc_mac_clean = "4C3BDF8C99F3"
        pc_mac_colon = "4C:3B:DF:8C:99:F3"
        switch_ip = "192.168.1.93"

        # Client phone is discovered in DB with device_type='phone' and is_mini_switch=True
        mock_repo.get_all_discovered_clients.return_value = {
            phone_mac_clean: {
                "mac": phone_mac_colon,
                "ip": "192.168.1.55",
                "hostname": "Desk Phone Cisco",
                "vendor": "Cisco Systems, Inc",
                "device_type": "phone",
                "is_mini_switch": 1,
                "passthrough_port": "PC",
                "switch_ip": "",
                "port": "2",
                "status": "online",
            },
            pc_mac_clean: {
                "mac": pc_mac_colon,
                "ip": "192.168.1.120",
                "hostname": "Microsoft Surface",
                "vendor": "Microsoft Corporation",
                "device_type": "laptop",
                "switch_ip": switch_ip,
                "port": "2",
                "status": "online",
            },
        }

        # Switch MAC table has both on Port 2
        polled_data = {
            switch_ip: {
                "status": "online",
                "mac": "AA0000000093",
                "ports": [
                    {"port": 2, "link": "up", "speed": "1G", "speed_tx_bps": 5000, "speed_rx_bps": 3000},
                ],
                "mac_table": [
                    {"port": 2, "mac": phone_mac_clean},
                    {"port": 2, "mac": pc_mac_clean},
                ],
            }
        }
        mock_poller.get_cached_data.return_value = polled_data

        topo_service = TopologyService(
            poller_service=mock_poller,
            vendor_service=mock_vendor,
            device_repo=mock_repo,
        )

        test_config = {
            "switches": [
                {"ip": switch_ip, "name": "Access Switch", "role": "access", "mac": "AA0000000093"},
            ],
            "clients": {},
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            topo = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes = {n["id"]: n for n in topo["nodes"]}
        phone_id = f"phone_{phone_mac_clean}"
        self.assertIn(phone_id, nodes)
        self.assertEqual(nodes[phone_id]["type"], "phone")
        self.assertEqual(nodes[phone_id]["parent_ip"], switch_ip)
        self.assertEqual(nodes[phone_id]["parent_port"], "Port 2")

        # Uplink: Access Switch Port 2 -> Phone
        uplink = next((l for l in topo["links"] if l["source"] == switch_ip and l["target"] == phone_id), None)
        self.assertIsNotNone(uplink)
        self.assertEqual(uplink["source_port"], "Port 2")

        # Downstream: Phone Port PC -> Microsoft Surface
        pc_link = next((l for l in topo["links"] if l["target"] == pc_mac_clean), None)
        self.assertIsNotNone(pc_link)
        self.assertEqual(pc_link["source"], phone_id)
        self.assertEqual(pc_link["source_port"], "PC")

        # Phone MAC is not duplicated as a client
        self.assertNotIn(phone_mac_clean, nodes)

    def test_ap_parent_switch_not_intercepted_by_phone(self):
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = "Test Vendor"

        switch_ip = "192.168.1.93"
        ap_ip = "10.20.1.5"
        phone_mac_clean = "A4934CFEF8C0"

        mock_poller.get_cached_data.return_value = {
            switch_ip: {
                "name": "Access Switch",
                "ip": switch_ip,
                "model": "edgecore",
                "role": "switch",
                "status": "online",
                "ports": [
                    {"port": 2, "speed": "1G", "link": "up", "speed_tx_bps": 1000, "speed_rx_bps": 2000},
                    {"port": 5, "speed": "100M", "link": "up", "speed_tx_bps": 5000, "speed_rx_bps": 6000},
                ],
                "mac_table": [
                    {"port": 2, "mac": phone_mac_clean},
                    {"port": 5, "mac": "F492BF299D0F"},
                ],
            },
            ap_ip: {
                "name": "UAP-AC-LR",
                "ip": ap_ip,
                "model": "unifi",
                "role": "access_point",
                "status": "online",
                "ports": [{"port": 1, "speed": "100M", "link": "up"}],
                "mac_table": [],
            },
        }
        mock_poller.get_cached_speeds.return_value = {}

        mock_repo = MagicMock()
        mock_repo.get_cached_switch_data.return_value = ({}, {})
        mock_repo.get_all_discovered_clients.return_value = {
            phone_mac_clean: {
                "mac": "A4:93:4C:FE:F8:C0",
                "ip": switch_ip,  # legacy/buggy row had switch_ip in ip column
                "switch_ip": switch_ip,
                "port": "2",
                "device_type": "phone",
                "is_mini_switch": 1,
                "passthrough_port": "PC",
                "status": "online",
            }
        }

        topo_service = TopologyService(
            poller_service=mock_poller,
            vendor_service=mock_vendor,
            device_repo=mock_repo,
        )

        test_config = {
            "switches": [
                {
                    "id": "sw_1",
                    "ip": switch_ip,
                    "name": "Access Switch",
                    "model": "edgecore",
                    "role": "switch",
                },
                {
                    "id": "ap_1",
                    "ip": ap_ip,
                    "name": "UAP-AC-LR",
                    "role": "access_point",
                    "device_type": "access_point",
                    "parent_ip": switch_ip,
                    "parent_port": "5",
                    "uplink_port": "1",
                },
            ],
            "clients": {},
        }

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            topo = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        phone_id = f"phone_{phone_mac_clean}"

        # AP must connect to Access Switch Port 5, NOT to Phone
        ap_uplink = next((l for l in topo["links"] if l["target"] == ap_ip), None)
        self.assertIsNotNone(ap_uplink)
        self.assertEqual(ap_uplink["source"], switch_ip)
        self.assertEqual(ap_uplink["source_port"], "Port 5")
        self.assertEqual(ap_uplink["target_port"], "Port 1")

        # Phone connects to Access Switch Port 2
        phone_uplink = next((l for l in topo["links"] if l["target"] == phone_id), None)
        self.assertIsNotNone(phone_uplink)
        self.assertEqual(phone_uplink["source"], switch_ip)
        self.assertEqual(phone_uplink["source_port"], "Port 2")

    def test_client_and_phone_ip_hostname_discovery(self):
        """Verifies that discovered clients and phone nodes are enriched with resolved IP and hostname,
        and that display name follows the hierarchy: Nickname > Discovered Hostname > Vendor.
        """
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = "Samsung Electronics"

        switch_ip = "192.168.1.10"
        client1_mac = "001122334401"
        client2_mac = "001122334402"
        phone_mac = "001122334403"

        cached_data = {
            switch_ip: {
                "mac": "AA0000000010",
                "ports": [
                    {"port": 1, "link": "up", "speed": "1G", "speed_tx_bps": 0, "speed_rx_bps": 0},
                    {"port": 2, "link": "up", "speed": "1G", "speed_tx_bps": 0, "speed_rx_bps": 0},
                    {"port": 3, "link": "up", "speed": "1G", "speed_tx_bps": 0, "speed_rx_bps": 0},
                ],
                "mac_table": [
                    {"port": 1, "mac": client1_mac, "ip": "192.168.1.101", "hostname": "smart-tv"},
                    {"port": 2, "mac": client2_mac, "ip": "192.168.1.102", "hostname": "livingroom-pc"},
                    {"port": 3, "mac": phone_mac, "ip": "192.168.1.103", "hostname": "cisco-deskphone"},
                ],
            }
        }
        mock_poller.get_cached_data.return_value = cached_data
        mock_poller.get_cached_speeds.return_value = {}

        test_config = {
            "switches": [
                {
                    "id": "sw1",
                    "ip": switch_ip,
                    "name": "Access Switch",
                    "model": "generic_switch",
                    "enabled": True,
                }
            ],
            "clients": {
                # client2 has a custom nickname override
                client2_mac: {
                    "mac": "00:11:22:33:44:02",
                    "host": "Custom Workstation",
                },
                phone_mac: {
                    "mac": "00:11:22:33:44:03",
                    "device_type": "phone",
                    "is_mini_switch": True,
                }
            }
        }

        topo_service = TopologyService(poller_service=mock_poller, vendor_service=mock_vendor)

        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            topo = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes_by_id = {n["id"]: n for n in topo["nodes"]}

        # Client 1: No nickname -> display name is discovered hostname "smart-tv"
        self.assertIn(client1_mac, nodes_by_id)
        c1 = nodes_by_id[client1_mac]
        self.assertEqual(c1["name"], "smart-tv")
        self.assertEqual(c1["hostname"], "smart-tv")
        self.assertEqual(c1["ip"], "192.168.1.101")
        self.assertEqual(c1.get("host", ""), "")

        # Client 2: Has nickname -> display name is "Custom Workstation", discovered hostname preserved
        self.assertIn(client2_mac, nodes_by_id)
        c2 = nodes_by_id[client2_mac]
        self.assertEqual(c2["name"], "Custom Workstation")
        self.assertEqual(c2["host"], "Custom Workstation")
        self.assertEqual(c2["hostname"], "livingroom-pc")
        self.assertEqual(c2["ip"], "192.168.1.102")

        # Phone node: Enriched with discovered IP & hostname
        phone_id = f"phone_{phone_mac}"
        self.assertIn(phone_id, nodes_by_id)
        phone_node = nodes_by_id[phone_id]
        self.assertEqual(phone_node["ip"], "192.168.1.103")
        self.assertEqual(phone_node["hostname"], "cisco-deskphone")

    def test_topology_without_ont_has_no_internet_node(self):
        """Tests that when no ONT is configured, no fake virtual Internet node is generated."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""

        router_ip = "192.168.1.1"
        mock_poller.get_cached_data.return_value = {
            router_ip: {
                "mac": "AA0000000001",
                "ports": [{"port": "1", "link": "up", "speed": "1G"}],
                "mac_table": [],
            }
        }
        test_config = {
            "switches": [
                {"ip": router_ip, "name": "Core Router", "role": "router", "mac": "AA0000000001"}
            ],
            "devices": [],
            "unmanaged_switches": [],
        }

        topo_service = TopologyService(poller_service=mock_poller, vendor_service=mock_vendor)
        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            topo = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes_by_id = {n["id"]: n for n in topo["nodes"]}
        self.assertNotIn("internet", nodes_by_id)
        self.assertIn(router_ip, nodes_by_id)
        self.assertFalse(any(l["source"] == "internet" or l["target"] == "internet" for l in topo["links"]))

    def test_topology_with_single_ont_and_asymmetric_pon_utilization(self):
        """Tests that configuring an ONT generates Internet -> ONT -> Router links with asymmetric PON speeds & telemetry."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""

        router_ip = "192.168.1.1"
        mock_poller.get_cached_data.return_value = {
            router_ip: {
                "mac": "AA0000000001",
                "ports": [
                    {
                        "port": "WAN",
                        "link": "up",
                        "speed": "1G",
                        "speed_rx_bps": 500_000_000,  # 500 Mbps down (50% of 1G)
                        "speed_tx_bps": 57_500_000,   # 57.5 Mbps up (50% of 115M)
                        "bytes_recv": 1234567,
                        "bytes_sent": 765432,
                    }
                ],
                "mac_table": [],
            }
        }
        test_config = {
            "switches": [
                {"ip": router_ip, "name": "Core Router", "role": "router", "mac": "AA0000000001"}
            ],
            "devices": [
                {
                    "id": "ont-cityfibre",
                    "name": "CityFibre ONT",
                    "role": "ont",
                    "device_type": "ont",
                    "parent_ip": router_ip,
                    "parent_port": "WAN",
                    "speed_down": "1G",
                    "speed_up": "115M",
                    "stats_device_ip": router_ip,
                    "stats_port": "WAN",
                }
            ],
        }

        topo_service = TopologyService(poller_service=mock_poller, vendor_service=mock_vendor)
        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            topo = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes_by_id = {n["id"]: n for n in topo["nodes"]}
        self.assertIn("internet", nodes_by_id)
        self.assertEqual(nodes_by_id["internet"]["type"], NodeRole.INTERNET)
        self.assertEqual(nodes_by_id["internet"]["ont_count"], 1)

        self.assertIn("ont-cityfibre", nodes_by_id)
        ont_node = nodes_by_id["ont-cityfibre"]
        self.assertEqual(ont_node["type"], NodeRole.ONT)
        self.assertEqual(ont_node["capacity_down_bps"], 1_000_000_000)
        self.assertEqual(ont_node["capacity_up_bps"], 115_000_000)
        self.assertEqual(ont_node["utilization_down_pct"], 50.0)
        self.assertEqual(ont_node["utilization_up_pct"], 50.0)
        self.assertEqual(ont_node["utilization_pct"], 50.0)

        # Internet -> ONT link
        wan_link = next((l for l in topo["links"] if l["id"] == "link_internet_ont-cityfibre"), None)
        self.assertIsNotNone(wan_link)
        self.assertEqual(wan_link["source"], "internet")
        self.assertEqual(wan_link["target"], "ont-cityfibre")
        self.assertEqual(wan_link["source_port"], "Cloud")
        self.assertEqual(wan_link["target_port"], "PON")
        self.assertEqual(wan_link["capacity_down_bps"], 1_000_000_000)
        self.assertEqual(wan_link["capacity_up_bps"], 115_000_000)
        self.assertEqual(wan_link["tx_bps"], 500_000_000)  # Internet tx is client download
        self.assertEqual(wan_link["rx_bps"], 57_500_000)   # Internet rx is client upload
        self.assertEqual(wan_link["utilization_down_pct"], 50.0)
        self.assertEqual(wan_link["utilization_up_pct"], 50.0)

        # ONT -> Router link
        downstream_link = next((l for l in topo["links"] if l["id"] == f"link_ont-cityfibre_{router_ip}"), None)
        self.assertIsNotNone(downstream_link)
        self.assertEqual(downstream_link["source"], "ont-cityfibre")
        self.assertEqual(downstream_link["target"], router_ip)
        self.assertEqual(downstream_link["source_port"], "LAN")
        self.assertEqual(downstream_link["target_port"], "WAN")

    def test_topology_with_multi_site_onts(self):
        """Tests multi-site topologies with multiple independent ONTs connecting to the Internet."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""

        router1_ip = "192.168.1.1"
        router2_ip = "10.0.0.1"

        mock_poller.get_cached_data.return_value = {
            router1_ip: {
                "mac": "AA0000000001",
                "ports": [{"port": "WAN", "link": "up", "speed": "1G", "speed_rx_bps": 100_000_000, "speed_tx_bps": 10_000_000}],
                "mac_table": [],
            },
            router2_ip: {
                "mac": "AA0000000002",
                "ports": [{"port": "WAN", "link": "up", "speed": "1G", "speed_rx_bps": 200_000_000, "speed_tx_bps": 50_000_000}],
                "mac_table": [],
            },
        }

        test_config = {
            "switches": [
                {"ip": router1_ip, "name": "Site A Router", "role": "router", "mac": "AA0000000001"},
                {"ip": router2_ip, "name": "Site B Router", "role": "router", "mac": "AA0000000002"},
            ],
            "devices": [
                {
                    "id": "ont-site-a",
                    "name": "Site A ONT",
                    "role": "ont",
                    "parent_ip": router1_ip,
                    "parent_port": "WAN",
                    "speed_down": "1G",
                    "speed_up": "100M",
                },
                {
                    "id": "ont-site-b",
                    "name": "Site B ONT",
                    "role": "ont",
                    "parent_ip": router2_ip,
                    "parent_port": "WAN",
                    "speed_down": "2.5G",
                    "speed_up": "2.5G",
                },
            ],
        }

        topo_service = TopologyService(poller_service=mock_poller, vendor_service=mock_vendor)
        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            topo = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes_by_id = {n["id"]: n for n in topo["nodes"]}
        self.assertIn("internet", nodes_by_id)
        self.assertEqual(nodes_by_id["internet"]["ont_count"], 2)

        # Both ONTs present
        self.assertIn("ont-site-a", nodes_by_id)
        self.assertIn("ont-site-b", nodes_by_id)

        links_by_id = {l["id"]: l for l in topo["links"]}
        self.assertIn("link_internet_ont-site-a", links_by_id)
        self.assertIn("link_internet_ont-site-b", links_by_id)
        self.assertIn(f"link_ont-site-a_{router1_ip}", links_by_id)
        self.assertIn(f"link_ont-site-b_{router2_ip}", links_by_id)

    def test_topology_with_ont_through_intermediate_switch_and_custom_stats(self):
        """Tests that an ONT connected to an intermediate switch can pull telemetry from a different device/port."""
        mock_poller = MagicMock()
        mock_vendor = MagicMock()
        mock_vendor.get_custom_vendors.return_value = {}
        mock_vendor.get_ieee_vendors.return_value = {}
        mock_vendor.lookup_vendor.return_value = ""

        switch_ip = "192.168.1.2"
        router_ip = "192.168.1.1"

        mock_poller.get_cached_data.return_value = {
            switch_ip: {
                "mac": "AA0000000002",
                "ports": [{"port": "1", "link": "up", "speed": "1G"}],
                "mac_table": [],
            },
            router_ip: {
                "mac": "AA0000000001",
                "ports": [
                    {
                        "port": "WAN",
                        "link": "up",
                        "speed": "1G",
                        "speed_rx_bps": 300_000_000,
                        "speed_tx_bps": 50_000_000,
                    }
                ],
                "mac_table": [],
            },
        }

        test_config = {
            "switches": [
                {"ip": router_ip, "name": "Router", "role": "router", "mac": "AA0000000001"},
                {"ip": switch_ip, "name": "Switch", "role": "switch", "mac": "AA0000000002"},
            ],
            "devices": [
                {
                    "id": "ont-dmz",
                    "name": "DMZ ONT",
                    "role": "ont",
                    "parent_ip": switch_ip,
                    "parent_port": "1",
                    "speed_down": "1G",
                    "speed_up": "100M",
                    "stats_device_ip": router_ip,
                    "stats_port": "WAN",
                }
            ],
        }

        topo_service = TopologyService(poller_service=mock_poller, vendor_service=mock_vendor)
        import switch_dashboard.services.topology_service as ts_mod
        orig_get_config = ts_mod.get_config
        ts_mod.get_config = lambda: test_config
        try:
            topo = topo_service.build_topology()
        finally:
            ts_mod.get_config = orig_get_config

        nodes_by_id = {n["id"]: n for n in topo["nodes"]}
        ont_node = nodes_by_id["ont-dmz"]
        self.assertEqual(ont_node["parent_ip"], switch_ip)
        self.assertEqual(ont_node["stats_device_ip"], router_ip)
        self.assertEqual(ont_node["rx_bps"], 300_000_000)
        self.assertEqual(ont_node["tx_bps"], 50_000_000)
        self.assertEqual(ont_node["utilization_down_pct"], 30.0)
        self.assertEqual(ont_node["utilization_up_pct"], 50.0)

        # Downstream link attaches to switch_ip port 1
        downstream = next(l for l in topo["links"] if l["id"] == f"link_ont-dmz_{switch_ip}")
        self.assertEqual(downstream["target"], switch_ip)
        self.assertEqual(downstream["target_port"], "Port 1")


if __name__ == "__main__":
    unittest.main()

