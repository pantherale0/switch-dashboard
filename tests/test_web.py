import tempfile
import unittest
from switch_dashboard.web import create_app
from switch_dashboard.config import set_data_dir, save_config, get_default_config


class TestWeb(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory(prefix="test_web_")
        set_data_dir(self.test_dir.name)
        save_config(get_default_config())
        self.app = create_app(start_background_workers=False)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.test_dir.cleanup()

    def test_pages(self):
        pages = ["/", "/map", "/network-map", "/config", "/logs", "/backups", "/scanner", "/scanner/history", "/api/docs"]
        for url in pages:
            res = self.client.get(url)
            self.assertEqual(res.status_code, 200, f"Page {url} returned {res.status_code}")

    def test_api_protocols(self):
        res = self.client.get("/api/protocols")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        names = [p["name"] for p in data]
        self.assertIn("http_hc", names)
        self.assertIn("ovs", names)
        self.assertIn("fritzbox", names)
        self.assertIn("snmp", names)

    def test_api_switches_and_speeds(self):
        res_sw = self.client.get("/api/switches")
        self.assertEqual(res_sw.status_code, 200)
        self.assertIsInstance(res_sw.get_json(), list)

        res_speed = self.client.get("/api/speeds")
        self.assertEqual(res_speed.status_code, 200)
        self.assertIsInstance(res_speed.get_json(), dict)

    def test_api_topology(self):
        res = self.client.get("/api/topology")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("nodes", data)
        self.assertIn("links", data)

    def test_api_history_validation(self):
        # Missing ip or port -> 400
        res = self.client.get("/api/history")
        self.assertEqual(res.status_code, 400)

        # Valid query -> 200
        res = self.client.get("/api/history?ip=192.168.1.1&port=1&range=live")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("tx", data)
        self.assertIn("rx", data)
        self.assertIn("timestamps", data)

    def test_api_notes(self):
        res = self.client.post("/api/notes", json={"key": "192.168.1.1:1", "note": "Test Note"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "ok")

    def test_api_settings(self):
        res = self.client.get("/api/settings")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("refresh_interval", data)

    def test_api_logs(self):
        res = self.client.get("/api/logs")
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.get_json(), list)

        res_lvl = self.client.post("/api/logs/level", json={"level": "DEBUG"})
        self.assertEqual(res_lvl.status_code, 200)
        self.assertEqual(res_lvl.get_json()["level"], "DEBUG")

    def test_api_scanner_hosts(self):
        res = self.client.get("/api/scanner/hosts")
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.get_json(), list)

    def test_api_clients_management(self):
        # Update host
        res_h = self.client.post("/api/clients/update_host", json={"mac": "AA:BB:CC:DD:EE:FF", "host": "NewHost"})
        self.assertEqual(res_h.status_code, 200)
        self.assertEqual(res_h.get_json()["status"], "ok")

        # Update type
        res_t = self.client.post("/api/clients/update_type", json={"mac": "AA:BB:CC:DD:EE:FF", "type": "desktop"})
        self.assertEqual(res_t.status_code, 200)
        self.assertEqual(res_t.get_json()["status"], "ok")

        # Delete client
        res_d = self.client.post("/api/clients/delete", json={"mac": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(res_d.status_code, 200)
        self.assertEqual(res_d.get_json()["status"], "ok")

    def test_api_history_ranges(self):
        for r in ["live", "1h", "24h"]:
            res = self.client.get(f"/api/history?ip=192.168.1.1&port=1&range={r}")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertIn("tx", data)
            self.assertIn("rx", data)
            self.assertIn("timestamps", data)


    def test_api_switches_capabilities_and_metadata(self):
        from switch_dashboard.services.poller_service import get_poller_service
        from switch_dashboard.config import get_default_config, save_config

        cfg = get_default_config()
        cfg["devices"] = [
            {"id": "dev1", "ip": "192.168.1.10", "name": "HTTP Switch", "protocol": "http_hc", "management_type": "managed", "enabled": True},
            {"id": "dev2", "ip": "192.168.1.20", "name": "SNMP Switch", "protocol": "snmp", "management_type": "managed", "enabled": True},
            {"id": "dev3", "ip": "192.168.1.30", "name": "Proxmox Cluster", "protocol": "proxmox", "device_type": "virtualisation_host", "management_type": "managed", "enabled": True},
        ]
        save_config(cfg)

        poller = get_poller_service()
        with poller._cache_lock:
            poller._cached_data["192.168.1.10"] = {"ip": "192.168.1.10", "name": "HTTP Switch", "ports": []}
            poller._cached_data["192.168.1.20"] = {"ip": "192.168.1.20", "name": "SNMP Switch", "ports": []}
            poller._cached_data["192.168.1.30"] = {"ip": "192.168.1.30", "name": "Proxmox Cluster", "ports": []}

        res = self.client.get("/api/switches")
        self.assertEqual(res.status_code, 200)
        data = {s["ip"]: s for s in res.get_json()}

        self.assertTrue(data["192.168.1.10"]["can_backup"])
        self.assertFalse(data["192.168.1.20"]["can_backup"])
        self.assertFalse(data["192.168.1.30"]["can_backup"])
        self.assertEqual(data["192.168.1.30"]["device_type"], "virtualisation_host")

    def test_dashboard_toolbar_elements(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("dashboard-toolbar", html)
        self.assertIn("active-ports-btn", html)
        self.assertIn("grid-columns-select", html)
        self.assertIn("renderVirtualisationCard", html)


if __name__ == "__main__":
    unittest.main()

