import time
import unittest
import tempfile
import os

from switch_dashboard.storage.database import Database
from switch_dashboard.storage.repositories.device_repo import DeviceRepository
from switch_dashboard.services.clients import (
    ClientMonitorService,
    LiveSpeed,
    clean_brand_name,
    signal_dbm_to_percent,
    compute_footprint_hash,
)


class TestClientMonitoring(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.test_dir.name, "test_clients.db")
        self.db = Database(self.db_path)
        self.db.init_db()
        self.repo = DeviceRepository(self.db)
        self.service = ClientMonitorService(self.db, self.repo)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_ip_mobility_tracking(self):
        mac = "AA:BB:CC:DD:EE:01"
        now = time.time()

        # First IP assignment
        self.repo.record_client_ip_observation(mac, "192.168.1.100", "my-laptop", now - 1000)
        history = self.repo.get_client_ip_history(mac)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["ip"], "192.168.1.100")
        self.assertTrue(history[0]["is_active"])

        # Client receives new IP via DHCP renewal / subnet migration
        self.repo.record_client_ip_observation(mac, "192.168.1.150", "my-laptop", now)
        history = self.repo.get_client_ip_history(mac)
        self.assertEqual(len(history), 2)
        
        active_ips = [h for h in history if h["is_active"]]
        inactive_ips = [h for h in history if not h["is_active"]]
        self.assertEqual(len(active_ips), 1)
        self.assertEqual(active_ips[0]["ip"], "192.168.1.150")
        self.assertEqual(len(inactive_ips), 1)
        self.assertEqual(inactive_ips[0]["ip"], "192.168.1.100")

    def test_connection_and_roaming_events(self):
        mac = "AA:BB:CC:DD:EE:02"
        now = time.time()

        # Connect to AP 1
        self.repo.record_client_connection_event(
            mac=mac,
            event_type="connected",
            switch_ip="192.168.1.20",
            switch_name="Living Room AP",
            port="ath0",
            ssid="Home-WiFi",
            signal_dbm=-55,
            connected_at=now - 300,
        )

        # Roam to AP 2
        self.repo.record_client_connection_event(
            mac=mac,
            event_type="roamed",
            switch_ip="192.168.1.21",
            switch_name="Office AP",
            port="ath1",
            ssid="Home-WiFi",
            signal_dbm=-62,
            from_switch_ip="192.168.1.20",
            from_port="ath0",
            connected_at=now,
        )

        events = self.repo.get_client_connection_history(mac)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_type"], "roamed")
        self.assertEqual(events[0]["switch_name"], "Office AP")
        self.assertEqual(events[0]["from_switch_ip"], "192.168.1.20")
        self.assertEqual(events[1]["event_type"], "connected")

    def test_bandwidth_metric_rollups(self):
        mac = "AA:BB:CC:DD:EE:03"
        now = time.time()

        # Record two metric samples in the same hour
        self.repo.record_client_metric_sample(
            mac=mac,
            delta_tx=1000,
            delta_rx=2000,
            speed_tx_bps=8000,
            speed_rx_bps=16000,
            timestamp=now,
        )
        self.repo.record_client_metric_sample(
            mac=mac,
            delta_tx=3000,
            delta_rx=4000,
            speed_tx_bps=24000,
            speed_rx_bps=32000,
            timestamp=now + 10,
        )

        trends = self.repo.get_client_bandwidth_trends(mac, hours=24)
        self.assertEqual(len(trends), 1)
        self.assertEqual(trends[0]["tx_bytes"], 4000)
        self.assertEqual(trends[0]["rx_bytes"], 6000)
        self.assertEqual(trends[0]["max_tx_bps"], 24000)
        self.assertEqual(trends[0]["max_rx_bps"], 32000)
        self.assertEqual(trends[0]["sample_count"], 2)

    def test_fing_device_classification(self):
        # 1. Laptop
        c_laptop = self.service.classify_device(
            vendor="Dell Inc.",
            hostname="Latitude-5420",
            device_type="laptop"
        )
        self.assertEqual(c_laptop["category"], "laptop")
        self.assertEqual(c_laptop["brand"], "Dell")

        # 2. Smartphone
        c_phone = self.service.classify_device(
            vendor="Apple, Inc.",
            hostname="Jordan-iPhone14",
            device_type="phone"
        )
        self.assertEqual(c_phone["category"], "smartphone")
        self.assertEqual(c_phone["brand"], "Apple")

        # 3. Smart Cleaner
        c_cleaner = self.service.classify_device(
            vendor="Anker Innovations Limited",
            hostname="RoboVac-30C",
            device_type="client"
        )
        self.assertEqual(c_cleaner["category"], "smart_home")
        self.assertEqual(c_cleaner["category_label"], "Smart Cleaner")

        # 4. Printer
        c_printer = self.service.classify_device(
            vendor="Lexmark International Inc.",
            hostname="ET0021B7D7F8BB",
            device_type="printer"
        )
        self.assertEqual(c_printer["category"], "printer")
        self.assertEqual(c_printer["brand"], "Lexmark")

        # 5. Router
        c_router = self.service.classify_device(
            vendor="AVM GmbH",
            hostname="fritz.box",
            device_type="router"
        )
        self.assertEqual(c_router["category"], "router")

    def test_signal_percentage_calculation(self):
        self.assertEqual(signal_dbm_to_percent(-50), 100)
        self.assertEqual(signal_dbm_to_percent(-55), 90)
        self.assertEqual(signal_dbm_to_percent(-65), 70)
        self.assertEqual(signal_dbm_to_percent(-100), 0)
        self.assertEqual(signal_dbm_to_percent(None), 0)

    def test_poller_cycle_processing_and_roaming(self):
        now = time.time()
        client_mac = "11:22:33:44:55:66"

        # Cycle 1: Client on AP 1
        results_1 = {
            "192.168.1.10": {
                "name": "Ground Floor AP",
                "status": "online",
                "mac_table": [
                    {
                        "mac": client_mac,
                        "ip": "192.168.1.45",
                        "hostname": "test-phone",
                        "port": "ath0",
                        "ssid": "MyOffice",
                        "signal": -60,
                        "tx_bytes": 10000,
                        "rx_bytes": 20000,
                    }
                ]
            }
        }
        self.service.process_polled_cycle(results_1, now)

        events = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "connected")
        self.assertEqual(events[0]["switch_name"], "Ground Floor AP")

        # Cycle 2: Client roams to AP 2
        results_2 = {
            "192.168.1.11": {
                "name": "First Floor AP",
                "status": "online",
                "mac_table": [
                    {
                        "mac": client_mac,
                        "ip": "192.168.1.45",
                        "hostname": "test-phone",
                        "port": "ath1",
                        "ssid": "MyOffice",
                        "signal": -52,
                        "tx_bytes": 15000,
                        "rx_bytes": 35000,
                    }
                ]
            }
        }
        self.service.process_polled_cycle(results_2, now + 15)

        events = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_type"], "roamed")
        self.assertEqual(events[0]["switch_name"], "First Floor AP")
        self.assertEqual(events[0]["from_switch_ip"], "192.168.1.10")

    def test_summary_and_subnets_aggregation(self):
        now = time.time()
        # Add clients in different subnets
        self.repo.upsert_discovered_clients_batch([
            {
                "mac": "00:11:22:33:44:01",
                "ip": "192.168.2.100",
                "hostname": "laptop-1",
                "vendor": "Dell Inc.",
                "device_type": "laptop",
                "status": "online",
                "last_seen_time": now,
            },
            {
                "mac": "00:11:22:33:44:02",
                "ip": "192.168.2.101",
                "hostname": "phone-1",
                "vendor": "Apple, Inc.",
                "device_type": "smartphone",
                "status": "online",
                "last_seen_time": now,
            },
            {
                "mac": "00:11:22:33:44:03",
                "ip": "10.0.0.50",
                "hostname": "server-1",
                "vendor": "Supermicro",
                "device_type": "server",
                "status": "offline",
                "last_seen_time": now - 3600,
            },
        ])

        subnets = self.service.get_subnets_summary()
        cidrs = [s["cidr"] for s in subnets]
        self.assertIn("192.168.2.0/24", cidrs)
        self.assertIn("10.0.0.0/24", cidrs)

        # Check Fing summary
        summary = self.service.get_monitoring_summary()
        self.assertEqual(summary["up_count"], 2)
        self.assertEqual(summary["total_discovered"], 3)
        self.assertEqual(summary["ratio_str"], "2 / 3")

    def test_edge_port_resolution_multi_switch(self):
        """Tests that a client seen across Router, Core, Distribution, and Access switches
        resolves exclusively to the physical access switch edge port with 0 noisy roamed events.
        """
        now = time.time()
        client_mac = "BC:24:11:19:59:69"

        # 4 devices report the client in the same cycle:
        # - Router on vlan02
        # - Core Switch on trunk port 8 (multiple MACs)
        # - Distribution Switch on trunk port 2 (multiple MACs)
        # - Access Switch on access port 9 (single MAC)
        cycle_results = {
            "192.168.1.254": {
                "name": "Router",
                "role": "router",
                "device_type": "router",
                "status": "online",
                "mac_table": [
                    {"mac": client_mac, "port": "vlan02", "ip": "192.168.1.88", "hostname": "Pioneer-AVR"},
                    {"mac": "AA:11:22:33:44:01", "port": "vlan02", "ip": "192.168.1.89"},
                    {"mac": "AA:11:22:33:44:02", "port": "vlan02", "ip": "192.168.1.90"},
                ],
            },
            "192.168.1.92": {
                "name": "Core Switch",
                "role": "switch",
                "device_type": "switch",
                "uplink_port": "8",
                "status": "online",
                "mac_table": [
                    {"mac": client_mac, "port": "8"},
                    {"mac": "AA:11:22:33:44:01", "port": "8"},
                    {"mac": "AA:11:22:33:44:02", "port": "8"},
                    {"mac": "AA:11:22:33:44:03", "port": "8"},
                ],
            },
            "192.168.1.90": {
                "name": "Distribution Switch",
                "role": "switch",
                "device_type": "switch",
                "status": "online",
                "mac_table": [
                    {"mac": client_mac, "port": "2"},
                    {"mac": "AA:11:22:33:44:01", "port": "2"},
                ],
            },
            "192.168.1.93": {
                "name": "Access Switch",
                "role": "switch",
                "device_type": "switch",
                "status": "online",
                "mac_table": [
                    # Port 9 has ONLY this client!
                    {"mac": client_mac, "port": "9"},
                ],
            },
        }

        # Cycle 1: Initial discovery
        self.service.process_polled_cycle(cycle_results, now)

        events = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "connected")
        self.assertEqual(events[0]["switch_ip"], "192.168.1.93")
        self.assertEqual(events[0]["switch_name"], "Access Switch")
        self.assertEqual(events[0]["port"], "9")

        # Discovered client should have Access Switch port 9 and the IP from the router
        discovered = self.repo.get_all_discovered_clients()
        mac_key = client_mac.replace(":", "")
        self.assertIn(mac_key, discovered)
        self.assertEqual(discovered[mac_key]["switch_ip"], "192.168.1.93")
        self.assertEqual(discovered[mac_key]["port"], "9")
        self.assertEqual(discovered[mac_key]["ip"], "192.168.1.88")
        self.assertEqual(discovered[mac_key]["hostname"], "Pioneer-AVR")

        # Subsequent Cycle 2 (15s later): Same state - MUST produce 0 roamed events
        self.service.process_polled_cycle(cycle_results, now + 15)
        events_cycle2 = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events_cycle2), 1)

        # Subsequent Cycle 3 (30s later): Same state - MUST produce 0 roamed events
        self.service.process_polled_cycle(cycle_results, now + 30)
        events_cycle3 = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events_cycle3), 1)

    def test_genuine_roam_between_switches(self):
        """Tests that moving a device from one access switch to another records exactly one roamed event."""
        now = time.time()
        client_mac = "BC:24:11:99:88:77"

        cycle_1 = {
            "192.168.1.93": {
                "name": "Access Switch 1",
                "role": "switch",
                "status": "online",
                "mac_table": [{"mac": client_mac, "port": "3"}],
            }
        }
        self.service.process_polled_cycle(cycle_1, now)

        events = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "connected")
        self.assertEqual(events[0]["switch_ip"], "192.168.1.93")
        self.assertEqual(events[0]["port"], "3")

        # Device moved to Access Switch 2, port 7
        cycle_2 = {
            "192.168.1.94": {
                "name": "Access Switch 2",
                "role": "switch",
                "status": "online",
                "mac_table": [{"mac": client_mac, "port": "7"}],
            }
        }
        self.service.process_polled_cycle(cycle_2, now + 30)

        events_after = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events_after), 2)
        self.assertEqual(events_after[0]["event_type"], "roamed")
        self.assertEqual(events_after[0]["switch_ip"], "192.168.1.94")
        self.assertEqual(events_after[0]["port"], "7")
        self.assertEqual(events_after[0]["from_switch_ip"], "192.168.1.93")
        self.assertEqual(events_after[0]["from_port"], "3")

    def test_rapid_flapping_debounce(self):
        """Tests that rapid flapping between ports within the debounce window is suppressed."""
        now = time.time()
        client_mac = "BC:24:11:44:55:66"

        # Cycle 1: Switch A
        self.service.process_polled_cycle({
            "192.168.1.93": {
                "name": "Switch A",
                "status": "online",
                "mac_table": [{"mac": client_mac, "port": "1"}],
            }
        }, now)

        # Cycle 2 (20s later): Roams to Switch B -> Allowed
        self.service.process_polled_cycle({
            "192.168.1.94": {
                "name": "Switch B",
                "status": "online",
                "mac_table": [{"mac": client_mac, "port": "2"}],
            }
        }, now + 20)

        events_after_roam = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events_after_roam), 2)
        self.assertEqual(events_after_roam[0]["event_type"], "roamed")

        # Cycle 3 (2s later): Rapid flip back to Switch A -> Debounced and ignored!
        self.service.process_polled_cycle({
            "192.168.1.93": {
                "name": "Switch A",
                "status": "online",
                "mac_table": [{"mac": client_mac, "port": "1"}],
            }
        }, now + 22)

        events_after_debounce = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events_after_debounce), 2)  # Still 2, no flapping roam added

    def test_cleanup_spurious_roamed_events(self):
        """Tests that historical spurious roamed events within 60s window or identical locations are cleaned."""
        now = time.time()
        client_mac = "BC:24:11:00:11:22"

        # 1. Initial connect
        self.repo.record_client_connection_event(
            mac=client_mac,
            event_type="connected",
            switch_ip="192.168.1.93",
            switch_name="Access Switch",
            port="9",
            connected_at=now - 200,
        )

        # 2. Spurious roamed event 1 second later to Distribution Switch
        self.repo.record_client_connection_event(
            mac=client_mac,
            event_type="roamed",
            switch_ip="192.168.1.90",
            switch_name="Distribution Switch",
            port="2",
            from_switch_ip="192.168.1.93",
            from_port="9",
            connected_at=now - 199,
        )

        # 3. Spurious roamed event 2 seconds later to Core Switch
        self.repo.record_client_connection_event(
            mac=client_mac,
            event_type="roamed",
            switch_ip="192.168.1.92",
            switch_name="Core Switch",
            port="8",
            from_switch_ip="192.168.1.90",
            from_port="2",
            connected_at=now - 198,
        )

        # 4. Spurious self-roam to same location
        self.repo.record_client_connection_event(
            mac=client_mac,
            event_type="roamed",
            switch_ip="192.168.1.92",
            switch_name="Core Switch",
            port="8",
            from_switch_ip="192.168.1.92",
            from_port="8",
            connected_at=now - 50,
        )

        before = self.repo.get_client_connection_history(client_mac)
        self.assertGreater(len(before), 1)

        # Execute cleanup
        cleaned = self.repo.cleanup_spurious_roamed_events(window_seconds=60.0)
        self.assertGreaterEqual(cleaned, 2)

        after = self.repo.get_client_connection_history(client_mac)
        # Only valid separated events should remain
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["event_type"], "connected")

    def test_footprint_dict_hashing_and_roaming(self):
        """Tests that client sightings across nodes are stored in a {node_id: port} dict,
        hashed deterministically, and only when the hash changes to a new edge port is a
        roaming event triggered and connected port updated.
        """
        # 1. Deterministic hashing verification
        f1 = {"sw_core": "8", "sw_dist": "2", "sw_access": "9"}
        f2 = {"sw_access": "9", "sw_core": "8", "sw_dist": "2"}
        f3 = {"sw_core": "8", "sw_dist": "2", "sw_access": "10"}

        h1 = compute_footprint_hash(f1)
        h2 = compute_footprint_hash(f2)
        h3 = compute_footprint_hash(f3)

        self.assertEqual(h1, h2, "Footprint hash must be strictly order-independent")
        self.assertNotEqual(h1, h3, "Footprint hash must change when any port changes")

        now = time.time()
        client_mac = "AA:BB:CC:77:88:99"

        # Cycle 1: Discovered across 3 nodes
        cycle_1 = {
            "192.168.1.92": {
                "name": "CORE",
                "role": "switch",
                "status": "online",
                "mac_table": [{"mac": client_mac, "port": "8"}, {"mac": "11:22:33:44:55:66", "port": "8"}],
            },
            "192.168.1.90": {
                "name": "DIST",
                "role": "switch",
                "status": "online",
                "mac_table": [{"mac": client_mac, "port": "2"}, {"mac": "11:22:33:44:55:66", "port": "2"}],
            },
            "192.168.1.93": {
                "name": "ACCESS_1",
                "role": "switch",
                "status": "online",
                "mac_table": [{"mac": client_mac, "port": "9"}],
            },
        }

        self.service.process_polled_cycle(cycle_1, now)

        # Verify initial hash and location
        self.assertIn(client_mac, self.service._last_footprint_hashes)
        initial_hash = self.service._last_footprint_hashes[client_mac]
        self.assertTrue(len(initial_hash) > 0)

        events = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "connected")
        self.assertEqual(events[0]["switch_ip"], "192.168.1.93")
        self.assertEqual(events[0]["port"], "9")

        # Cycle 2 (15s later): Identical sightings footprint
        self.service.process_polled_cycle(cycle_1, now + 15)
        # Hash should not have changed
        self.assertEqual(self.service._last_footprint_hashes[client_mac], initial_hash)
        # Zero roaming events required
        events_cycle2 = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events_cycle2), 1)

        # Cycle 3 (30s later): Client moves to ACCESS_2 on port 5
        cycle_3 = {
            "192.168.1.92": {
                "name": "CORE",
                "role": "switch",
                "status": "online",
                "mac_table": [
                    {"mac": client_mac, "port": "8"},
                    {"mac": "11:22:33:44:55:66", "port": "8"},
                ],
            },
            "192.168.1.90": {
                "name": "DIST",
                "role": "switch",
                "status": "online",
                "mac_table": [
                    {"mac": client_mac, "port": "4"},
                    {"mac": "11:22:33:44:55:66", "port": "4"},
                ],
            },
            "192.168.1.94": {
                "name": "ACCESS_2",
                "role": "switch",
                "status": "online",
                "mac_table": [{"mac": client_mac, "port": "5"}],
            },
        }

        self.service.process_polled_cycle(cycle_3, now + 35)

        # Hash must have changed
        new_hash = self.service._last_footprint_hashes[client_mac]
        self.assertNotEqual(initial_hash, new_hash)

        # Roaming event required and connected port changed to ACCESS_2:5
        events_cycle3 = self.repo.get_client_connection_history(client_mac)
        self.assertEqual(len(events_cycle3), 2)
        self.assertEqual(events_cycle3[0]["event_type"], "roamed")
        self.assertEqual(events_cycle3[0]["switch_ip"], "192.168.1.94")
        self.assertEqual(events_cycle3[0]["port"], "5")
        self.assertEqual(events_cycle3[0]["from_switch_ip"], "192.168.1.93")
        self.assertEqual(events_cycle3[0]["from_port"], "9")

        # Discovered client connected port updated
        discovered = self.repo.get_all_discovered_clients()
        c = discovered[client_mac.replace(":", "")]
        self.assertEqual(c["switch_ip"], "192.168.1.94")
        self.assertEqual(c["port"], "5")

    def test_livespeed_compatibility_in_ui_methods(self):
        """Verifies that LiveSpeed objects in _live_speeds work seamlessly with
        get_monitoring_summary, get_client_list, and get_client_details without throwing
        AttributeError or KeyError.
        """
        mac = "00:AA:BB:CC:DD:EE"
        now = time.time()
        self.repo.upsert_discovered_clients_batch([
            {
                "mac": mac,
                "ip": "10.10.51.5",
                "hostname": "test-workstation",
                "vendor": "Dell Inc.",
                "status": "online",
                "last_seen_time": now,
            }
        ])

        # Test dict-like methods on LiveSpeed directly
        ls = LiveSpeed(speed_tx_bps=1000, speed_rx_bps=2000, cum_tx=50000, cum_rx=60000, ts=now)
        self.assertEqual(ls.get("speed_tx_bps"), 1000)
        self.assertEqual(ls["speed_rx_bps"], 2000)
        self.assertEqual(ls.get("nonexistent", 999), 999)

        # Set in service's live speeds
        self.service._live_speeds[mac] = ls

        # Test get_monitoring_summary
        summary = self.service.get_monitoring_summary(subnet_filter="10.10.51.0/24")
        self.assertEqual(summary["up_count"], 1)
        self.assertEqual(summary["total_down_bps"], 2000)
        self.assertEqual(summary["total_up_bps"], 1000)

        # Test get_client_list
        clients = self.service.get_client_list(subnet_filter="10.10.51.0/24")
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["speed_tx_bps"], 1000)
        self.assertEqual(clients[0]["speed_rx_bps"], 2000)

        # Test get_client_details
        details = self.service.get_client_details(mac)
        self.assertIsNotNone(details)
        self.assertEqual(details["bandwidth"]["speed_tx_bps"], 1000)
        self.assertEqual(details["bandwidth"]["speed_rx_bps"], 2000)


if __name__ == "__main__":
    unittest.main()


