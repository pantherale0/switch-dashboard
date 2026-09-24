import unittest
import time
from switch_dashboard.storage.database import Database
from switch_dashboard.storage.repositories.device_repo import DeviceRepository
from switch_dashboard.storage.repositories.metric_repo import MetricRepository
from switch_dashboard.storage.repositories.scanner_repo import ScannerRepository
from switch_dashboard.storage.housekeeper import Housekeeper


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.init_db()
        self.device_repo = DeviceRepository(self.db)
        self.metric_repo = MetricRepository(self.db)
        self.scanner_repo = ScannerRepository(self.db)
        self.housekeeper = Housekeeper(self.db)

    def test_device_repository(self):
        sw = {
            "ip": "192.168.1.50",
            "name": "Core Switch",
            "model": "HC-SWTGW218AS",
            "enabled": True,
            "port_count": 8,
        }
        self.device_repo.upsert_device(sw, last_seen=time.time())

        dev = self.device_repo.get_device_by_ip("192.168.1.50")
        self.assertIsNotNone(dev)
        self.assertEqual(dev["name"], "Core Switch")
        self.assertEqual(dev["model"], "HC-SWTGW218AS")

        # Test MAC table
        macs = [
            {"port": "1", "mac": "AA:BB:CC:DD:EE:01", "vlan": "10"},
            {"port": "2", "mac": "AA:BB:CC:DD:EE:02", "vlan": "20"},
        ]
        self.device_repo.update_mac_table("192.168.1.50", macs, time.time())
        retrieved_macs = self.device_repo.get_mac_table("192.168.1.50")
        self.assertEqual(len(retrieved_macs), 2)
        self.assertEqual(retrieved_macs[0]["mac"], "AA:BB:CC:DD:EE:01")

    def test_mac_table_duplicate_entries_does_not_fail(self):
        # Exact reproduction of user's issue with duplicate (port, mac) pairs
        dup_macs = [
            {"port": "9", "mac": "3C:61:05:82:DF:5D", "vlan": "1"},
            {"port": "9", "mac": "3C:61:05:82:DF:5D", "vlan": "2"},  # Duplicate port & mac across VLANs
            {"port": "9", "mac": "BC:24:11:56:3A:92", "vlan": "2"},
            {"port": "9", "mac": "BC:24:11:56:3A:92", "vlan": "2"},  # Identical duplicate entry
            {"port": "8", "mac": "BC:24:11:19:59:69", "vlan": "2"},
        ]
        # Must not raise sqlite3.IntegrityError
        self.device_repo.update_mac_table("192.168.1.93", dup_macs, time.time())
        retrieved = self.device_repo.get_mac_table("192.168.1.93")
        self.assertEqual(len(retrieved), 3)
        mac_set = {r["mac"] for r in retrieved}
        self.assertIn("3C:61:05:82:DF:5D", mac_set)
        self.assertIn("BC:24:11:56:3A:92", mac_set)
        self.assertIn("BC:24:11:19:59:69", mac_set)

    def test_metric_repository_counters(self):
        counters = {
            "192.168.1.50:1": {
                "tx": 1000,
                "rx": 2000,
                "cum_tx": 5000,
                "cum_rx": 6000,
                "ts": 123456.0,
            }
        }
        self.metric_repo.save_counters(counters)
        loaded = self.metric_repo.load_counters()
        self.assertIn("192.168.1.50:1", loaded)
        self.assertEqual(loaded["192.168.1.50:1"]["cum_tx"], 5000)

    def test_repair_negative_counters(self):
        with self.db.transaction() as cur:
            cur.execute("""
                INSERT INTO counters (device_ip, port, tx_bytes, rx_bytes, cum_tx, cum_rx, timestamp)
                VALUES ('192.168.1.92', '7', 44000000000, 91000000000, 18000000000, -121978186161, 1000.0)
            """)

        self.metric_repo.repair_negative_counters()
        loaded = self.metric_repo.load_counters()
        self.assertIn("192.168.1.92:7", loaded)
        # Negative cum_rx should be self-healed to rx_bytes (91000000000)
        self.assertEqual(loaded["192.168.1.92:7"]["cum_rx"], 91000000000)
        self.assertEqual(loaded["192.168.1.92:7"]["cum_tx"], 18000000000)

    def test_zabbix_style_housekeeper_aggregation(self):
        # Insert raw samples timestamped 3 hours ago (older than 2h rollup cutoff)
        now = time.time()
        hour_base = int((now - 10800) / 3600) * 3600
        samples = [
            {
                "device_ip": "192.168.1.50",
                "port": "1",
                "ts": hour_base + 60,
                "tx_bytes": 1000,
                "rx_bytes": 500,
                "speed_tx_bps": 100_000,
                "speed_rx_bps": 50_000,
            },
            {
                "device_ip": "192.168.1.50",
                "port": "1",
                "ts": hour_base + 120,
                "tx_bytes": 2000,
                "rx_bytes": 1000,
                "speed_tx_bps": 200_000,
                "speed_rx_bps": 150_000,
            },
            {
                "device_ip": "192.168.1.50",
                "port": "1",
                "ts": hour_base + 180,
                "tx_bytes": 3000,
                "rx_bytes": 1500,
                "speed_tx_bps": 300_000,
                "speed_rx_bps": 100_000,
            },
        ]
        self.metric_repo.record_samples_batch(samples)

        # Run housekeeper
        stats = self.housekeeper.run_once()
        self.assertGreaterEqual(stats["trends_aggregated"], 1)

        # Verify trend bucket aggregated correctly: min, max, avg
        with self.db.connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT sample_count, avg_tx_bps, max_tx_bps, min_tx_bps, avg_rx_bps, max_rx_bps, min_rx_bps
                FROM metric_trends
                WHERE device_ip = '192.168.1.50' AND port = '1'
            """)
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["sample_count"], 3)
            self.assertEqual(row["min_tx_bps"], 100_000)
            self.assertEqual(row["max_tx_bps"], 300_000)
            self.assertEqual(row["avg_tx_bps"], 200_000)
            self.assertEqual(row["min_rx_bps"], 50_000)
            self.assertEqual(row["max_rx_bps"], 150_000)
            self.assertEqual(row["avg_rx_bps"], 100_000)

    def test_scanner_repository(self):
        curr_scan = {
            "192.168.1.10": {"mac": "11:22:33:44:55:66"}
        }
        last_state = {}
        res = self.scanner_repo.update_db_and_get_status(
            current_scan_results=curr_scan,
            last_db_state=last_state,
            ports_to_scan={80, 443},
            perform_port_scan=False,
            port_scan_enabled=False,
            port_scan_timeout=0.5,
            port_scan_threads=2,
            vendor_lookup_fn=lambda mac: "TestVendor",
            port_scan_fn=None,
        )
        self.assertIn("192.168.1.10", res)
        self.assertEqual(res["192.168.1.10"]["vendor"], "TestVendor")
        self.assertEqual(res["192.168.1.10"]["status"], "Online")

        hosts = self.scanner_repo.get_all_hosts_list()
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["ip_address"], "192.168.1.10")

        self.scanner_repo.update_host("192.168.1.10", hostname="Server1", note="Rack 1")
        updated = self.scanner_repo.load_state_from_db()
        self.assertEqual(updated["192.168.1.10"]["hostname"], "Server1")
        self.assertEqual(updated["192.168.1.10"]["note"], "Rack 1")

    def test_device_state_cache(self):
        results = {
            "192.168.1.50": {
                "name": "Core Switch",
                "ip": "192.168.1.50",
                "status": "online",
                "ports": [{"port": "1", "speed": "1G", "link": "up"}],
            }
        }
        speeds = {"192.168.1.50": {"1": {"speed_tx": 1000, "speed_rx": 2000}}}
        self.device_repo.save_all_cached_device_states(results, speeds)

        loaded_data, loaded_speeds = self.device_repo.load_all_cached_device_states()
        self.assertIn("192.168.1.50", loaded_data)
        self.assertEqual(loaded_data["192.168.1.50"]["name"], "Core Switch")
        self.assertIn("192.168.1.50", loaded_speeds)
        self.assertEqual(loaded_speeds["192.168.1.50"]["1"]["speed_tx"], 1000)

    def test_discovered_clients(self):
        now = time.time()
        clients = [
            {
                "mac": "00:11:22:33:44:55",
                "ip": "192.168.1.101",
                "host": "MyLaptop",
                "vendor": "Dell",
                "device_type": "laptop",
                "switch_ip": "192.168.1.50",
                "port": "1",
                "first_seen": now - 100,
                "last_seen": now,
            }
        ]
        self.device_repo.upsert_discovered_clients_batch(clients)

        all_clients = self.device_repo.get_all_discovered_clients()
        self.assertIn("001122334455", all_clients)
        c = all_clients["001122334455"]
        self.assertEqual(c["hostname"], "MyLaptop")
        self.assertEqual(c["device_type"], "laptop")
        self.assertEqual(c["port"], "1")

        self.device_repo.update_discovered_client_meta("00:11:22:33:44:55", host="OfficeLaptop", device_type="desktop")
        all_clients = self.device_repo.get_all_discovered_clients()
        self.assertEqual(all_clients["001122334455"]["hostname"], "OfficeLaptop")
        self.assertEqual(all_clients["001122334455"]["device_type"], "desktop")

        self.device_repo.delete_discovered_client("00:11:22:33:44:55")
        all_clients = self.device_repo.get_all_discovered_clients()
        self.assertNotIn("001122334455", all_clients)

    def test_metric_repository_history_range(self):
        now = time.time()
        samples = [
            {
                "device_ip": "192.168.1.50",
                "port": "1",
                "ts": now - 1800,
                "tx_bytes": 1000,
                "rx_bytes": 500,
                "speed_tx_bps": 150_000,
                "speed_rx_bps": 75_000,
            },
            {
                "device_ip": "192.168.1.50",
                "port": "1",
                "ts": now - 900,
                "tx_bytes": 2000,
                "rx_bytes": 1000,
                "speed_tx_bps": 250_000,
                "speed_rx_bps": 125_000,
            },
            {
                "device_ip": "192.168.1.50",
                "port": "1",
                "ts": now - 60,
                "tx_bytes": 3000,
                "rx_bytes": 1500,
                "speed_tx_bps": 350_000,
                "speed_rx_bps": 175_000,
            },
        ]
        self.metric_repo.record_samples_batch(samples)

        # 1h range
        h1 = self.metric_repo.get_history_range("192.168.1.50", "1", "1h")
        self.assertEqual(len(h1["tx"]), 3)
        self.assertEqual(h1["tx"][0], 150_000)
        self.assertEqual(h1["tx"][1], 250_000)
        self.assertEqual(h1["tx"][2], 350_000)

        # 24h range (bucketed)
        h24 = self.metric_repo.get_history_range("192.168.1.50", "1", "24h")
        self.assertGreaterEqual(len(h24["tx"]), 1)

        # Live range fallback to recent DB samples when in-memory buffer is fresh
        hlive = self.metric_repo.get_history_range("192.168.1.50", "1", "live")
        self.assertGreaterEqual(len(hlive["tx"]), 1)

    def test_database_url_and_engine_config(self):
        import os
        from switch_dashboard.storage.engine import get_database_url

        # Default SQLite URL
        url = get_database_url()
        self.assertTrue(url.startswith("sqlite:///"))

        # Custom DATABASE_URL environment override (including postgres:// normalization)
        old_val = os.environ.get("DATABASE_URL")
        try:
            os.environ["DATABASE_URL"] = "postgres://user:pass@localhost:5432/testdb"
            pg_url = get_database_url()
            self.assertEqual(pg_url, "postgresql://user:pass@localhost:5432/testdb")
        finally:
            if old_val is not None:
                os.environ["DATABASE_URL"] = old_val
            else:
                os.environ.pop("DATABASE_URL", None)

    def test_config_repository_crud_and_import(self):
        import os
        import tempfile
        import json
        from switch_dashboard.storage.engine import get_engine
        from switch_dashboard.storage.models import Base
        from switch_dashboard.storage.repositories.config_repo import ConfigRepository

        # Use an isolated in-memory SQLite database
        test_url = "sqlite:///:memory:"
        engine = get_engine(test_url)
        Base.metadata.create_all(engine)

        repo = ConfigRepository(db_url=test_url)
        self.assertFalse(repo.has_any_config())

        # Test import from legacy JSON
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            json.dump({
                "devices": [
                    {"id": "dev_test_1", "ip": "10.0.0.1", "name": "Test Switch 1", "model": "generic_switch", "management_type": "managed"},
                    {"id": "dev_test_2", "ip": "10.0.0.2", "name": "Desk Phone", "model": "cisco", "device_type": "phone", "management_type": "unmanaged"},
                ],
                "clients": {
                    "001122334455": {"mac": "00:11:22:33:44:55", "host": "Test Client"}
                },
                "port_notes": {
                    "10.0.0.1:1": "Uplink to Core"
                },
                "settings": {"polling_interval": 15}
            }, tf)
            temp_json_path = tf.name

        try:
            imported = repo.import_from_json_if_empty(temp_json_path)
            self.assertTrue(imported)
            self.assertTrue(repo.has_any_config())

            # Verify loaded structure
            loaded = repo.load_full_config()
            self.assertEqual(len(loaded["devices"]), 2)
            self.assertEqual(len(loaded["switches"]), 1)
            self.assertEqual(len(loaded["unmanaged_switches"]), 1)
            self.assertIn("001122334455", loaded["clients"])
            self.assertEqual(loaded["clients"]["001122334455"]["host"], "Test Client")
            self.assertIn("10.0.0.1:1", loaded["port_notes"])
            self.assertEqual(loaded["port_notes"]["10.0.0.1:1"], "Uplink to Core")
            self.assertEqual(loaded["settings"].get("polling_interval"), 15)

            # Test updating notes
            repo.save_notes({"10.0.0.1:2": "New Server"})
            notes = repo.get_notes()
            self.assertIn("10.0.0.1:2", notes)
            self.assertEqual(notes["10.0.0.1:2"], "New Server")

            # Test save_full_config
            loaded["settings"]["polling_interval"] = 30
            repo.save_full_config(loaded)
            reloaded = repo.load_full_config()
            self.assertEqual(reloaded["settings"]["polling_interval"], 30)

        finally:
            if os.path.exists(temp_json_path):
                os.remove(temp_json_path)
            if os.path.exists(temp_json_path + ".bak"):
                os.remove(temp_json_path + ".bak")

    def test_repositories_with_db_url_and_extended_features(self):
        from switch_dashboard.storage.engine import get_engine
        from switch_dashboard.storage.models import Base

        mem_url = "sqlite:///:memory:"
        engine = get_engine(mem_url)
        Base.metadata.create_all(engine)

        dev_repo = DeviceRepository(db_url=mem_url)
        metric_repo = MetricRepository(db_url=mem_url)
        scan_repo = ScannerRepository(db_url=mem_url)

        # 1. Device repo mini-switch & passthrough test
        dev_repo.upsert_discovered_clients_batch([{
            "mac": "11:22:33:44:55:66",
            "ip": "192.168.1.200",
            "host": "DeskPhone",
            "device_type": "phone",
        }])
        dev_repo.update_discovered_client_passthrough("11:22:33:44:55:66", is_mini_switch=True, passthrough_port="PC-Port")
        clients = dev_repo.get_all_discovered_clients()
        self.assertIn("112233445566", clients)
        phone = clients["112233445566"]
        self.assertEqual(phone["is_mini_switch"], 1)
        self.assertEqual(phone["passthrough_port"], "PC-Port")

        # 2. Scanner repo history & toggle known host
        scan_repo.update_db_and_get_status(
            current_scan_results={"192.168.1.15": {"mac": "AA:BB:CC:DD:EE:FF"}},
            last_db_state={},
            ports_to_scan=None,
            perform_port_scan=False,
            port_scan_enabled=False,
            port_scan_timeout=1.0,
            port_scan_threads=1,
        )
        scan_repo.toggle_known_host("192.168.1.15", is_known=True)
        st = scan_repo.load_state_from_db()
        self.assertEqual(st["192.168.1.15"]["known_host"], 1)

        hist = scan_repo.get_all_history()
        self.assertGreaterEqual(len(hist), 1)
        scan_repo.clear_all_history()
        self.assertEqual(len(scan_repo.get_all_history()), 0)

        # 3. Metric repo counters reset
        metric_repo.save_counters({"192.168.1.1:1": {"tx": 100, "rx": 200, "cum_tx": 100, "cum_rx": 200, "ts": 1.0}})
        self.assertIn("192.168.1.1:1", metric_repo.load_counters())
        metric_repo.reset_all_counters()
        self.assertEqual(len(metric_repo.load_counters()), 0)


if __name__ == "__main__":
    unittest.main()


