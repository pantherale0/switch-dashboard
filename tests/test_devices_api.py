import json
import tempfile
import unittest
from unittest.mock import patch

from switch_dashboard.web import create_app
from switch_dashboard.config import load_config, save_config, set_data_dir, get_default_config


class TestDevicesAPI(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory(prefix="test_devices_api_")
        set_data_dir(self.test_dir.name)
        save_config(get_default_config())
        self.app = create_app(start_background_workers=False)
        self.app.testing = True
        self.client = self.app.test_client()
        self.poll_patcher = patch("switch_dashboard.web.blueprints.config.trigger_background_poll")
        self.mock_poll = self.poll_patcher.start()

    def tearDown(self):
        self.poll_patcher.stop()
        self.test_dir.cleanup()
        import tests.conftest as tc
        set_data_dir(getattr(tc, "_TEST_DATA_DIR", tempfile.gettempdir()))

    def test_get_devices_api(self):
        res = self.client.get("/api/devices")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("devices", data)
        self.assertIsInstance(data["devices"], list)

    def test_add_and_delete_device_crud(self):
        # 1. Add a managed device
        new_managed = {
            "name": "Distribution Switch East",
            "ip": "192.168.1.99",
            "management_type": "managed",
            "device_type": "switch",
            "protocol": "http_hc",
            "username": "admin",
            "password": "secretpassword",
            "parent_ip": "192.168.1.1",
            "parent_port": "Port 2",  # Should be normalized to 2
            "uplink_port": "Port 1",  # Should be normalized to 1
        }
        res = self.client.post("/api/devices", json=new_managed)
        self.assertEqual(res.status_code, 201)
        created = res.get_json()["device"]
        self.assertEqual(created["name"], "Distribution Switch East")
        self.assertEqual(str(created["parent_port"]), "2")
        self.assertEqual(str(created["uplink_port"]), "1")
        dev_id = created["id"]

        # 2. Update device
        update_payload = {
            "name": "Distribution Switch East Updated",
            "parent_port": 3,
        }
        res_put = self.client.put(f"/api/devices/{dev_id}", json=update_payload)
        self.assertEqual(res_put.status_code, 200)
        updated = res_put.get_json()["device"]
        self.assertEqual(updated["name"], "Distribution Switch East Updated")
        self.assertEqual(str(updated["parent_port"]), "3")

        # 3. Delete device
        res_del = self.client.delete(f"/api/devices/{dev_id}")
        self.assertEqual(res_del.status_code, 200)

        # 4. Verify gone
        res_get = self.client.get("/api/devices")
        ids = [d["id"] for d in res_get.get_json()["devices"]]
        self.assertNotIn(dev_id, ids)

    def test_add_unmanaged_device(self):
        new_unmanaged = {
            "name": "Desk 5-Port Unmanaged",
            "management_type": "unmanaged",
            "device_type": "unmanaged_switch",
            "parent_ip": "192.168.1.90",
            "parent_port": "Port 4",  # Should be normalized to 4
        }
        res = self.client.post("/api/devices", json=new_unmanaged)
        self.assertEqual(res.status_code, 201)
        created = res.get_json()["device"]
        self.assertEqual(str(created["parent_port"]), "4")
        dev_id = created["id"]

        # Clean up
        self.client.delete(f"/api/devices/{dev_id}")

    def test_validate_protocol_config_route(self):
        # Valid http_hc
        res = self.client.post("/api/protocols/http_hc/validate", json={"username": "admin", "password": "pw"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["valid"])

        # Invalid http_hc (invalid timeout)
        res_bad = self.client.post("/api/protocols/http_hc/validate", json={"http_timeout": "invalid_int"})
        self.assertEqual(res_bad.status_code, 400)
        self.assertFalse(res_bad.get_json()["valid"])

    def test_test_connection_endpoint(self):
        # Missing IP
        res_missing = self.client.post("/api/devices/test-connection", json={})
        self.assertEqual(res_missing.status_code, 400)

        # Valid payload with mocked driver
        from switch_dashboard.protocols.registry import ProtocolRegistry
        with patch.object(ProtocolRegistry, "create") as mock_create:
            mock_driver = mock_create.return_value
            mock_driver.test_connection.return_value = (True, "Connection OK")

            res_mock = self.client.post(
                "/api/devices/test-connection",
                json={"ip": "192.168.1.90", "protocol": "http_hc", "username": "admin", "password": "admin"}
            )
            self.assertEqual(res_mock.status_code, 200)
            data = res_mock.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["message"], "Connection OK")

    def test_detect_upstream_endpoint(self):
        res = self.client.get("/api/devices/detect-upstream?ip=192.168.1.100")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("found", data)

    def test_config_post_with_devices_json(self):
        payload_devices = [
            {
                "id": "dev_test_post",
                "name": "Integration Test Switch",
                "ip": "192.168.1.88",
                "management_type": "managed",
                "device_type": "switch",
                "protocol": "http_hc",
                "username": "admin",
                "password": "pw",
                "parent_ip": "192.168.1.1",
                "parent_port": "Port 3",
            }
        ]
        res = self.client.post(
            "/config",
            data={
                "title": "Switch Dashboard",
                "devices_json": json.dumps(payload_devices),
                "refresh_interval": "30",
            },
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)

        cfg = load_config()
        # Verify devices in config
        dev_names = [d["name"] for d in cfg.get("devices", [])]
        self.assertIn("Integration Test Switch", dev_names)

        # Verify dual projection into legacy switches
        sw_names = [s["name"] for s in cfg.get("switches", [])]
        self.assertIn("Integration Test Switch", sw_names)

        # Clean up test device
        cfg["devices"] = [d for d in cfg["devices"] if d.get("id") != "dev_test_post"]
        save_config(cfg)

    def test_switch_role_consolidation(self):
        for legacy_role in ["core_switch", "dist_switch", "access_switch"]:
            payload = {
                "name": f"Test {legacy_role}",
                "ip": "192.168.1.150",
                "management_type": "managed",
                "role": legacy_role,
                "device_type": legacy_role,
                "protocol": "http_hc",
                "username": "admin",
                "password": "pw",
            }
            res = self.client.post("/api/devices", json=payload)
            self.assertEqual(res.status_code, 201)
            device = res.get_json()["device"]
            self.assertEqual(device["role"], "switch")
            self.assertEqual(device["device_type"], "switch")

            # Clean up
            self.client.delete(f"/api/devices/{device['id']}")

    def test_add_proxmox_virtualisation_host(self):
        payload = {
            "name": "Production Cluster",
            "ip": "192.168.1.20",
            "management_type": "managed",
            "role": "virtualisation_host",
            "device_type": "virtualisation_host",
            "protocol": "proxmox",
            "model": "proxmox",
            "api_user": "dashboard@pve",
            "token_id": "switch-dashboard",
            "token_secret": "secret",
            "api_port": "8006",
            "verify_ssl": False,
        }

        response = self.client.post("/api/devices", json=payload)
        self.assertEqual(response.status_code, 201)
        device = response.get_json()["device"]
        self.assertEqual(device["role"], "virtualisation_host")
        self.assertEqual(device["device_type"], "virtualisation_host")
        self.assertEqual(device["protocol"], "proxmox")
        self.assertEqual(device["api_port"], 8006)

        cfg = load_config()
        projected = next(sw for sw in cfg["switches"] if sw.get("id") == device["id"])
        self.assertEqual(projected["role"], "virtualisation_host")

    def test_phone_mini_switch_configuration_and_upstream_candidates(self):
        # 1. Add Phone as unmanaged device with mini-switch enabled
        payload = {
            "name": "Front Desk Phone",
            "ip": "192.168.1.75",
            "mac": "00:04:13:aa:bb:cc",
            "management_type": "unmanaged",
            "device_type": "phone",
            "role": "phone",
            "parent_ip": "192.168.1.1",
            "parent_port": 5,
            "is_mini_switch": True,
            "passthrough_port": "PC",
        }
        res = self.client.post("/api/devices", json=payload)
        self.assertEqual(res.status_code, 201)
        created = res.get_json()["device"]
        self.assertEqual(created["device_type"], "phone")
        self.assertEqual(created["role"], "phone")
        self.assertTrue(created.get("is_mini_switch"))
        self.assertEqual(created.get("passthrough_port"), "PC")

        # 2. Verify it is NOT projected into switches (so it never appears as a full switch card on the dashboard)
        cfg = load_config()
        switch_ids = [s.get("id") for s in cfg.get("switches", [])]
        self.assertNotIn(created["id"], switch_ids)

        # 3. Verify it appears in /api/devices/upstream-candidates
        res_cand = self.client.get("/api/devices/upstream-candidates")
        self.assertEqual(res_cand.status_code, 200)
        candidates = res_cand.get_json()["candidates"]
        phone_cand = next((c for c in candidates if c.get("id") == created["id"]), None)
        self.assertIsNotNone(phone_cand)
        self.assertTrue(phone_cand.get("is_phone"))
        self.assertTrue(phone_cand.get("is_mini_switch"))
        self.assertEqual(phone_cand.get("passthrough_port"), "PC")

    def test_client_update_passthrough_toggle(self):
        # 1. Update discovered client device to act as mini switch
        test_mac = "00:15:65:12:34:56"
        res = self.client.post(
            "/api/clients/update_passthrough",
            json={"mac": test_mac, "is_mini_switch": True},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["is_mini_switch"])

        # 2. Check it appears in upstream candidates
        res_cand = self.client.get("/api/devices/upstream-candidates")
        candidates = res_cand.get_json()["candidates"]
        cand = next((c for c in candidates if c.get("mac") == test_mac), None)
        self.assertIsNotNone(cand)
        self.assertTrue(cand.get("is_mini_switch"))

        # 3. Toggle off
        res_off = self.client.post(
            "/api/clients/update_passthrough",
            json={"mac": test_mac, "is_mini_switch": False},
        )
        self.assertEqual(res_off.status_code, 200)
        self.assertFalse(res_off.get_json()["is_mini_switch"])

    def test_ont_device_crud_api(self):
        # 1. Create an ONT unmanaged device with asymmetric speeds and stats mapping
        new_ont = {
            "name": "Openreach Full Fibre ONT",
            "management_type": "unmanaged",
            "device_type": "ont",
            "role": "ont",
            "parent_ip": "192.168.1.254",
            "parent_port": "WAN",
            "speed_down": "1000M",
            "speed_up": "115M",
            "speed": "1000M / 115M",
            "stats_device_ip": "192.168.1.254",
            "stats_port": "WAN",
        }
        res = self.client.post("/api/devices", json=new_ont)
        self.assertEqual(res.status_code, 201)
        created = res.get_json()["device"]
        self.assertEqual(created["name"], "Openreach Full Fibre ONT")
        self.assertEqual(created["device_type"], "ont")
        self.assertEqual(created["speed_down"], "1000M")
        self.assertEqual(created["speed_up"], "115M")
        self.assertEqual(created["stats_device_ip"], "192.168.1.254")
        self.assertEqual(created["stats_port"], "WAN")
        ont_id = created["id"]

        # 2. Verify GET /api/devices returns the ONT device
        res_get = self.client.get("/api/devices")
        self.assertEqual(res_get.status_code, 200)
        devices = res_get.get_json()["devices"]
        ont_in_list = next((d for d in devices if d.get("id") == ont_id), None)
        self.assertIsNotNone(ont_in_list)
        self.assertEqual(ont_in_list["speed_down"], "1000M")
        self.assertEqual(ont_in_list["speed_up"], "115M")

        # 3. Update the ONT device
        update_payload = {
            "speed_down": "2.5G",
            "speed_up": "1G",
            "name": "Upgraded XGS-PON ONT",
        }
        res_update = self.client.put(f"/api/devices/{ont_id}", json=update_payload)
        self.assertEqual(res_update.status_code, 200)
        updated = res_update.get_json()["device"]
        self.assertEqual(updated["name"], "Upgraded XGS-PON ONT")
        self.assertEqual(updated["speed_down"], "2.5G")
        self.assertEqual(updated["speed_up"], "1G")

        # 4. Clean up / Delete
        res_del = self.client.delete(f"/api/devices/{ont_id}")
        self.assertEqual(res_del.status_code, 200)
