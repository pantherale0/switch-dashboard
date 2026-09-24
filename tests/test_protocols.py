import unittest
from typing import Dict, Any, Tuple, List
from switch_dashboard.protocols.base import BaseProtocol, Capability
from switch_dashboard.protocols.registry import ProtocolRegistry, register_protocol
from switch_dashboard.protocols import scrape_switch


class TestProtocols(unittest.TestCase):
    def test_builtin_protocols_registered(self):
        protocols = ProtocolRegistry.list_protocols()
        names = [p["name"] for p in protocols]
        self.assertIn("http_hc", names)
        self.assertIn("ovs", names)
        self.assertIn("fritzbox", names)
        self.assertIn("snmp", names)

    def test_protocol_factory_by_explicit_name(self):
        sw = {"ip": "10.0.0.1", "protocol": "snmp"}
        proto = ProtocolRegistry.create(sw)
        self.assertEqual(proto.protocol_name, "snmp")
        self.assertTrue(proto.has_capability(Capability.METRICS))

    def test_protocol_factory_by_model_fallback(self):
        sw_ovs = {"ip": "10.0.0.2", "model": "openvswitch"}
        proto_ovs = ProtocolRegistry.create(sw_ovs)
        self.assertEqual(proto_ovs.protocol_name, "ovs")

        sw_fb = {"ip": "10.0.0.3", "model": "fritzbox"}
        proto_fb = ProtocolRegistry.create(sw_fb)
        self.assertEqual(proto_fb.protocol_name, "fritzbox")

        sw_generic = {"ip": "10.0.0.4", "model": "Generic Model"}
        proto_generic = ProtocolRegistry.create(sw_generic)
        self.assertEqual(proto_generic.protocol_name, "http_hc")

    def test_dynamic_custom_protocol_registration(self):
        @register_protocol(
            name="mock_netconf",
            display_name="Mock NETCONF Protocol",
            supported_models=["mock_router"]
        )
        class MockNetconfProtocol(BaseProtocol):
            name = "mock_netconf"
            capabilities = {Capability.METRICS, Capability.BACKUP}

            def test_connection(self) -> Tuple[bool, str]:
                return True, "Mock OK"

            def scrape(self) -> Dict[str, Any]:
                return {"ip": self.ip, "status": "online", "ports": []}

            def scrape_mac_table(self) -> List[Dict[str, Any]]:
                return [{"port": "1", "mac": "11:22:33:44:55:66"}]

            def download_backup(self) -> bytes:
                return b"<xml>backup</xml>"

        # Verify registration in registry
        cls = ProtocolRegistry.get_protocol_class("mock_netconf")
        self.assertIsNotNone(cls)
        self.assertEqual(cls, MockNetconfProtocol)

        # Verify creation via model
        proto = ProtocolRegistry.create({"ip": "10.0.0.5", "model": "mock_router"})
        self.assertEqual(proto.name, "mock_netconf")
        self.assertTrue(proto.has_capability(Capability.BACKUP))
        self.assertFalse(proto.has_capability(Capability.REBOOT))

        data = proto.scrape()
        self.assertEqual(data["status"], "online")
        backup = proto.download_backup()
        self.assertEqual(backup, b"<xml>backup</xml>")

    def test_snmp_driver_scrape_and_port_structure(self):
        from switch_dashboard.protocols.drivers.snmp.driver import SNMPProtocol
        # Test with mock ports
        snmp_mock = SNMPProtocol({
            "ip": "10.0.0.10",
            "name": "SNMP Mock Switch",
            "mock_ports": [{"port": 1, "link": "up", "speed": "1G"}],
        })
        scraped = snmp_mock.scrape()
        self.assertEqual(scraped["status"], "online")
        self.assertEqual(len(scraped["ports"]), 1)
        p = scraped["ports"][0]
        self.assertEqual(p["port"], "1")
        self.assertEqual(p["status"], "up")
        self.assertIn(p["link"].lower(), ["up", "link up"])

        # Test offline handling
        snmp_offline = SNMPProtocol({
            "ip": "127.0.0.1",
            "snmp_port": 19999,  # nothing listening
            "community": "public",
            "port_count": 4,
        })
        offline_data = snmp_offline.scrape()
        self.assertEqual(offline_data["status"], "offline")
        self.assertEqual(len(offline_data["ports"]), 4)
        for port in offline_data["ports"]:
            self.assertIn("status", port)
            self.assertEqual(port["status"], "down")
            self.assertIn("link", port)
            self.assertEqual(port["link"], "Link Down")

        # Test test_connection return type
        ok, msg = snmp_offline.test_connection()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(msg, str)

    def test_snmp_custom_oid_configuration(self):
        from switch_dashboard.protocols.drivers.snmp.driver import (
            SNMPProtocol,
            OID_SYS_DESCR,
            OID_DOT1D_TP_FDB_ADDRESS,
        )

        # 1. Verify default OID assignments
        default_driver = SNMPProtocol({"ip": "10.0.0.20", "community": "public"})
        self.assertEqual(default_driver.oid_sys_descr, OID_SYS_DESCR)
        self.assertEqual(default_driver.oid_fdb_address, OID_DOT1D_TP_FDB_ADDRESS)

        # 2. Verify custom OID overrides
        custom_cfg = {
            "ip": "10.0.0.21",
            "protocol": "snmp",
            "community": "secret_comm",
            "oid_sys_descr": "1.3.6.1.4.1.9.1.1",
            "oid_sys_uptime": "1.3.6.1.4.1.9.1.2",
            "oid_sys_name": "1.3.6.1.4.1.9.1.3",
            "oid_if_descr": "1.3.6.1.4.1.9.2.1",
            "oid_if_oper_status": "1.3.6.1.4.1.9.2.2",
            "oid_if_speed": "1.3.6.1.4.1.9.2.3",
            "oid_if_in_octets": "1.3.6.1.4.1.9.2.4",
            "oid_if_out_octets": "1.3.6.1.4.1.9.2.5",
            "oid_fdb_address": "1.3.6.1.4.1.9.3.1",
            "oid_fdb_port": "1.3.6.1.4.1.9.3.2",
            "oid_lldp_rem_chassis": "1.3.6.1.4.1.9.4.1",
            "oid_lldp_rem_port": "1.3.6.1.4.1.9.4.2",
            "oid_lldp_rem_sysname": "1.3.6.1.4.1.9.4.3",
        }

        # Validate with Voluptuous via ProtocolRegistry
        is_valid, error, coerced = ProtocolRegistry.validate_config("snmp", custom_cfg)
        self.assertTrue(is_valid, f"Validation failed: {error}")
        self.assertIsNone(error)
        self.assertEqual(coerced["oid_sys_descr"], "1.3.6.1.4.1.9.1.1")

        custom_driver = ProtocolRegistry.create(coerced)
        self.assertEqual(custom_driver.oid_sys_descr, "1.3.6.1.4.1.9.1.1")
        self.assertEqual(custom_driver.oid_sys_uptime, "1.3.6.1.4.1.9.1.2")
        self.assertEqual(custom_driver.oid_sys_name, "1.3.6.1.4.1.9.1.3")
        self.assertEqual(custom_driver.oid_if_descr, "1.3.6.1.4.1.9.2.1")
        self.assertEqual(custom_driver.oid_if_oper_status, "1.3.6.1.4.1.9.2.2")
        self.assertEqual(custom_driver.oid_if_speed, "1.3.6.1.4.1.9.2.3")
        self.assertEqual(custom_driver.oid_if_in_octets, "1.3.6.1.4.1.9.2.4")
        self.assertEqual(custom_driver.oid_if_out_octets, "1.3.6.1.4.1.9.2.5")
        self.assertEqual(custom_driver.oid_fdb_address, "1.3.6.1.4.1.9.3.1")
        self.assertEqual(custom_driver.oid_fdb_port, "1.3.6.1.4.1.9.3.2")
        self.assertEqual(custom_driver.oid_lldp_rem_chassis, "1.3.6.1.4.1.9.4.1")
        self.assertEqual(custom_driver.oid_lldp_rem_port, "1.3.6.1.4.1.9.4.2")
        self.assertEqual(custom_driver.oid_lldp_rem_sysname, "1.3.6.1.4.1.9.4.3")

    def test_snmp_schema_ui_metadata(self):
        protocols = ProtocolRegistry.list_protocols()
        snmp_meta = next((p for p in protocols if p["name"] == "snmp"), None)
        self.assertIsNotNone(snmp_meta)
        schema_names = [f["name"] for f in snmp_meta["config_schema"]]
        self.assertIn("community", schema_names)
        self.assertIn("snmp_port", schema_names)
        self.assertIn("oid_sys_descr", schema_names)
        self.assertIn("oid_if_descr", schema_names)
        self.assertIn("oid_fdb_address", schema_names)
        self.assertIn("oid_dot1q_fdb_port", schema_names)
        self.assertIn("oid_lldp_rem_sysname", schema_names)

    def test_snmp_qbridge_mac_table_walk(self):
        from switch_dashboard.protocols.drivers.snmp.driver import SNMPProtocol
        driver = SNMPProtocol({"ip": "10.0.0.30", "community": "public"})
        def fake_walk(oid, timeout=2.0):
            if oid == driver.oid_dot1q_fdb_port:
                return True, [("1.3.6.1.2.1.17.7.1.2.2.1.2.6.0.2.201.16.238.100", 2)]
            return False, []
        driver._snmp_walk = fake_walk
        table = driver.scrape_mac_table()
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0]["mac"], "00:02:C9:10:EE:64")
        self.assertEqual(table[0]["port"], "2")
        self.assertEqual(table[0]["vlan"], "6")


if __name__ == "__main__":
    unittest.main()
