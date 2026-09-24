import os
import tempfile
import time
import unittest

from switch_dashboard.services.clients.mobility import MobilityTracker
from switch_dashboard.services.clients.models import ResolvedEdge, clean_port_name
from switch_dashboard.storage.database import Database
from switch_dashboard.storage.repositories.device_repo import DeviceRepository


class TestClientMobility(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.test_dir.name, "test_mobility.db")
        self.db = Database(self.db_path)
        self.db.init_db()
        self.repo = DeviceRepository(self.db)
        self.mobility = MobilityTracker(device_repo=self.repo, min_roam_debounce_seconds=15.0)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_first_connection_event(self):
        mac = "00:11:22:33:44:55"
        now = time.time()
        resolved = ResolvedEdge(
            mac=mac,
            node_id="192.168.1.10",
            node_name="Switch1",
            port="3",
            vlan="10",
            ssid="",
            signal_dbm=None,
            is_child=False,
        )
        footprint = {"192.168.1.10": "3"}
        footprint_hash = "abc123hash"

        self.mobility.evaluate_mobility(mac, resolved, footprint, footprint_hash, now)

        history = self.repo.get_client_connection_history(mac)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["event_type"], "connected")
        self.assertEqual(history[0]["switch_ip"], "192.168.1.10")
        self.assertEqual(history[0]["port"], "3")

    def test_roaming_and_debounce_suppression(self):
        mac = "00:11:22:33:44:55"
        t0 = 1000.0

        # 1. Connected to AP 1
        res1 = ResolvedEdge(
            mac=mac,
            node_id="192.168.1.20",
            node_name="AP_LivingRoom",
            port="ath0",
            vlan="1",
            ssid="HomeNet",
            signal_dbm=-50,
            is_child=False,
        )
        self.mobility.evaluate_mobility(mac, res1, {"192.168.1.20": "ath0"}, "hash1", t0)
        self.assertEqual(len(self.repo.get_client_connection_history(mac)), 1)

        # 2. Roams to AP 2 5 seconds later (< 15s debounce window)
        res2 = ResolvedEdge(
            mac=mac,
            node_id="192.168.1.21",
            node_name="AP_Kitchen",
            port="ath0",
            vlan="1",
            ssid="HomeNet",
            signal_dbm=-55,
            is_child=False,
        )
        self.mobility.evaluate_mobility(mac, res2, {"192.168.1.21": "ath0"}, "hash2", t0 + 5.0)

        # Roam should be debounced / suppressed
        events = self.repo.get_client_connection_history(mac)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["switch_ip"], "192.168.1.20")

        # 3. Roams to AP 2 20 seconds later (>= 15s debounce window)
        self.mobility.evaluate_mobility(mac, res2, {"192.168.1.21": "ath0"}, "hash2", t0 + 25.0)
        events_roamed = self.repo.get_client_connection_history(mac)
        self.assertEqual(len(events_roamed), 2)
        self.assertEqual(events_roamed[0]["event_type"], "roamed")
        self.assertEqual(events_roamed[0]["switch_ip"], "192.168.1.21")
        self.assertEqual(events_roamed[0]["from_switch_ip"], "192.168.1.20")
        self.assertEqual(events_roamed[0]["from_port"], "ath0")

    def test_ping_pong_port_flapping_dampened(self):
        """Verifies that when a device rapidly alternates between ports (ping-pong bounce),
        flapping dampening suppresses the spurious roam events and stabilizes the location.
        """
        mac = "00:AA:BB:11:22:33"
        t0 = 2000.0

        edge_port_1 = ResolvedEdge(mac=mac, node_id="sw1", port="1")
        edge_port_2 = ResolvedEdge(mac=mac, node_id="sw1", port="2")

        # 1. Initial connect on Port 1
        ev1, _ = self.mobility.evaluate_mobility(mac, edge_port_1, {"sw1": "1"}, "hash1", t0)
        self.assertEqual(ev1, "connected")

        # 2. Legitimate move to Port 2 20s later
        ev2, _ = self.mobility.evaluate_mobility(mac, edge_port_2, {"sw1": "2"}, "hash2", t0 + 20.0)
        self.assertEqual(ev2, "roamed")

        # 3. Flap / bounce back to Port 1 20s later (ping-pong back to recently visited port)
        ev3, loc3 = self.mobility.evaluate_mobility(mac, edge_port_1, {"sw1": "1"}, "hash1", t0 + 40.0)
        self.assertEqual(ev3, "flapping_dampened", "Rapid ping-pong port hopping must be dampened")
        # Client location should remain stable at Port 2, avoiding ping-pong in UI
        self.assertEqual(loc3.port, "2")

        # History should only have the 2 legitimate events (connected and first roam), not the bounce
        events = self.repo.get_client_connection_history(mac)
        self.assertEqual(len(events), 2)

    def test_clean_port_name(self):
        self.assertEqual(clean_port_name("Port 1"), "1")
        self.assertEqual(clean_port_name("port1"), "1")
        self.assertEqual(clean_port_name("Port-24"), "24")
        self.assertEqual(clean_port_name(" 8 "), "8")
        self.assertEqual(clean_port_name("Gi1/0/1"), "Gi1/0/1")
        self.assertEqual(clean_port_name("eth0"), "eth0")
        self.assertEqual(clean_port_name(None), "")


if __name__ == "__main__":
    unittest.main()
