import unittest
from switch_dashboard.web import create_app
from switch_dashboard.storage.database import get_db
from switch_dashboard.storage.repositories.device_repo import DeviceRepository


class TestClientsApi(unittest.TestCase):
    def setUp(self):
        self.app = create_app(config_override={"settings": {"refresh_interval": 60}}, start_background_workers=False)
        self.app.testing = True
        self.client = self.app.test_client()
        self.db = get_db()
        self.repo = DeviceRepository(self.db)

    def test_clients_page_render(self):
        res = self.client.get("/clients")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Active Client Monitoring", res.data)
        self.assertIn(b"Detected Networks", res.data)
        self.assertIn(b"Up / Discovered", res.data)

    def test_subnets_api(self):
        res = self.client.get("/api/clients/subnets")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "ok")
        self.assertIsInstance(data.get("subnets"), list)

    def test_summary_api(self):
        res = self.client.get("/api/clients/monitoring/summary")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "ok")
        summary = data.get("summary")
        self.assertIn("up_count", summary)
        self.assertIn("total_discovered", summary)
        self.assertIn("ratio_str", summary)
        self.assertIn("popular_type", summary)
        self.assertIn("popular_vendor", summary)

    def test_clients_list_api(self):
        res = self.client.get("/api/clients/monitoring/list")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "ok")
        self.assertIn("clients", data)
        self.assertIsInstance(data.get("clients"), list)

    def test_client_details_api(self):
        mac = "00:11:22:33:44:55"
        self.repo.upsert_discovered_clients_batch([
            {
                "mac": mac,
                "ip": "192.168.1.55",
                "hostname": "test-workstation",
                "vendor": "Dell Inc.",
                "device_type": "laptop",
                "status": "online",
            }
        ])

        res = self.client.get(f"/api/clients/{mac}/details")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "ok")
        client = data.get("client")
        self.assertEqual(client.get("mac"), mac)
        self.assertEqual(client.get("ip"), "192.168.1.55")
        self.assertIn("network_setup", client)
        self.assertIn("wifi_setup", client)
        self.assertIn("bandwidth", client)
        self.assertIn("connection_history", client)
        self.assertIn("ip_history", client)

    def test_client_edit_nickname_api(self):
        mac = "00:11:22:33:44:66"
        self.repo.upsert_discovered_clients_batch([
            {
                "mac": mac,
                "ip": "192.168.1.66",
                "hostname": "old-host",
                "status": "online",
            }
        ])

        res = self.client.post(
            f"/api/clients/{mac}/edit",
            json={"custom_name": "Executive Laptop"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("status"), "ok")

        # Verify details reflects custom name
        res_details = self.client.get(f"/api/clients/{mac}/details")
        self.assertEqual(res_details.status_code, 200)
        client = res_details.get_json().get("client")
        self.assertEqual(client.get("custom_name"), "Executive Laptop")
        self.assertEqual(client.get("display_name"), "Executive Laptop")


if __name__ == "__main__":
    unittest.main()
