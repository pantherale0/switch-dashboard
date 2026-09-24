import unittest
from unittest.mock import MagicMock
from switch_dashboard.services.poller_service import unify_speed, format_bps, calculate_counter_delta
from switch_dashboard.services.topology_service import normalize_mac, is_ignored_mac
from switch_dashboard.services.scanner_service import parse_port_range
from switch_dashboard.services.vendor_service import VendorService


class TestServices(unittest.TestCase):
    def test_unify_speed(self):
        self.assertEqual(unify_speed("1000M"), "1G")
        self.assertEqual(unify_speed("1000"), "1G")
        self.assertEqual(unify_speed("2500M"), "2.5G")
        self.assertEqual(unify_speed("10G"), "10G")
        self.assertEqual(unify_speed("100M"), "100M")
        self.assertEqual(unify_speed(""), "")

    def test_format_bps(self):
        self.assertEqual(format_bps(500), "500 bps")
        self.assertEqual(format_bps(1500), "1.5 Kbps")
        self.assertEqual(format_bps(25_000_000), "25.0 Mbps")
        self.assertEqual(format_bps(1_000_000_000), "1.0 Gbps")
        self.assertEqual(format_bps(10_000_000_000), "10.0 Gbps")

    def test_normalize_mac(self):
        self.assertEqual(normalize_mac("aa:bb:cc:dd:ee:ff"), "AABBCCDDEEFF")
        self.assertEqual(normalize_mac("AA-BB-CC-DD-EE-FF"), "AABBCCDDEEFF")
        self.assertEqual(normalize_mac(""), "")

    def test_is_ignored_mac(self):
        cfg = {"settings": {"ignored_macs": ["00:11:22:*", "AA:BB:CC:DD:EE:FF"]}}
        self.assertTrue(is_ignored_mac("00:11:22:33:44:55", cfg))
        self.assertTrue(is_ignored_mac("AA:BB:CC:DD:EE:FF", cfg))
        self.assertFalse(is_ignored_mac("11:22:33:44:55:66", cfg))

    def test_parse_port_range(self):
        ports = parse_port_range("22, 80, 443, 8000-8002")
        self.assertEqual(ports, {22, 80, 443, 8000, 8001, 8002})

        empty = parse_port_range("")
        self.assertEqual(empty, set())

    def test_vendor_service_lookup(self):
        vs = VendorService()
        vs._custom_cache = {"001122": "CustomBrand"}
        vs._ieee_cache = {"AABBCC": "IEEEBrand"}

        self.assertEqual(vs.lookup_vendor("00:11:22:33:44:55"), "CustomBrand")
        self.assertEqual(vs.lookup_vendor("AA:BB:CC:11:22:33"), "IEEEBrand")
        self.assertEqual(vs.lookup_vendor("99:99:99:99:99:99"), "")

        # Test multi-argument call: lookup_vendor(mac, vendors, ieee_vendors)
        custom_dict = {"112233": "OverrideBrand"}
        ieee_dict = {"445566": "ExplicitIEEE"}
        self.assertEqual(vs.lookup_vendor("11:22:33:44:55:66", custom_dict, ieee_dict), "OverrideBrand")
        self.assertEqual(vs.lookup_vendor("44:55:66:77:88:99", custom_dict, ieee_dict), "ExplicitIEEE")

    def test_calculate_counter_delta(self):
        limit = 150_000_000

        # Normal increase
        self.assertEqual(calculate_counter_delta(2000, 1000, limit), 1000)

        # 32-bit wrap rollover
        wrapped_delta = calculate_counter_delta(50_000, 4_200_000_000, limit)
        self.assertEqual(wrapped_delta, (4_294_967_296 - 4_200_000_000) + 50_000)
        self.assertGreater(wrapped_delta, 0)

        # 64-bit counter decrease (device reset or reboot) - MUST NEVER be negative!
        self.assertEqual(calculate_counter_delta(500, 90_000_000_000, limit), 500)
        self.assertEqual(calculate_counter_delta(0, 90_000_000_000, limit), 0)

        # Impossible delta exceeding link capacity
        self.assertEqual(calculate_counter_delta(90_000_000_000, 100, limit), 0)

    def test_host_discovery_kernel_arp(self):
        from unittest.mock import patch, mock_open
        from switch_dashboard.services.host_discovery_service import HostDiscoveryService

        proc_arp_content = (
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.1.100    0x1         0x2         aa:bb:cc:dd:ee:01     *        eth0\n"
            "192.168.1.101    0x1         0x2         aa:bb:cc:dd:ee:02     *        eth0\n"
            "192.168.1.102    0x1         0x0         00:00:00:00:00:00     *        eth0\n"
        )
        service = HostDiscoveryService(db=MagicMock())
        with patch("builtins.open", mock_open(read_data=proc_arp_content)):
            table = service.get_kernel_arp_table()

        self.assertEqual(table.get("AABBCCDDEE01"), "192.168.1.100")
        self.assertEqual(table.get("AABBCCDDEE02"), "192.168.1.101")
        self.assertNotIn("000000000000", table)

    def test_host_discovery_clean_hostname(self):
        from switch_dashboard.services.host_discovery_service import HostDiscoveryService

        service = HostDiscoveryService(db=MagicMock())
        self.assertEqual(service.clean_hostname("myhost.local.", "192.168.1.50"), "myhost.local")
        self.assertEqual(service.clean_hostname("192.168.1.50", "192.168.1.50"), "")
        self.assertEqual(service.clean_hostname("_gateway", "192.168.1.1"), "")
        self.assertEqual(service.clean_hostname(None), "")

    def test_host_discovery_dns_caching(self):
        from unittest.mock import patch
        from switch_dashboard.services.host_discovery_service import HostDiscoveryService

        service = HostDiscoveryService(db=MagicMock())
        with patch("socket.gethostbyaddr", return_value=("printer.local.", [], ["10.0.0.88"])) as mock_dns:
            # First lookup performs query
            h1 = service.resolve_hostname_sync("10.0.0.88")
            self.assertEqual(h1, "printer.local")
            self.assertEqual(mock_dns.call_count, 1)

            # Second lookup returns from cache without calling gethostbyaddr again
            h2 = service.resolve_hostname_sync("10.0.0.88")
            self.assertEqual(h2, "printer.local")
            self.assertEqual(mock_dns.call_count, 1)

    def test_host_discovery_resolve_all_clients(self):
        from unittest.mock import patch
        from switch_dashboard.services.host_discovery_service import HostDiscoveryService

        service = HostDiscoveryService(db=MagicMock())
        switches_by_ip = {
            "192.168.1.1": {
                "mac_table": [
                    {"mac": "11:22:33:44:55:66", "ip": "192.168.1.55", "hostname": "fritz-nas"},
                    {"mac": "AA:BB:CC:DD:EE:FF", "ip": "192.168.1.1"},
                ],
                "child_devices": [
                    {
                        "name": "pve-vm-100",
                        "macs": ["22:33:44:55:66:77"],
                        "ips": ["192.168.1.200"],
                        "interfaces": [{"mac": "22:33:44:55:66:77", "ip": "192.168.1.200"}],
                    }
                ]
            }
        }
        with patch.object(service, "get_kernel_arp_table", return_value={"334455667788": "192.168.1.188"}), \
             patch.object(service, "get_scanner_hosts", return_value={}):
            resolved = service.resolve_all_clients(switches_by_ip, infra_ips={"192.168.1.1"})

        # Fritz!box mac_table entry
        self.assertEqual(resolved["112233445566"]["ip"], "192.168.1.55")
        self.assertEqual(resolved["112233445566"]["hostname"], "fritz-nas")

        # Proxmox child device
        self.assertEqual(resolved["223344556677"]["ip"], "192.168.1.200")
        self.assertEqual(resolved["223344556677"]["hostname"], "pve-vm-100")

        # Kernel ARP
        self.assertEqual(resolved["334455667788"]["ip"], "192.168.1.188")

        # Infrastructure IP excluded from client
        self.assertEqual(resolved["AABBCCDDEEFF"]["ip"], "")


if __name__ == "__main__":
    unittest.main()
