import unittest
from switch_dashboard.services.clients.models import ClientSighting
from switch_dashboard.services.clients.resolver import EdgeResolver


class TestClientResolver(unittest.TestCase):
    def setUp(self):
        self.resolver = EdgeResolver()

    def test_child_container_precedence(self):
        mac = "02:00:00:00:00:01"
        sightings = [
            ClientSighting(node_id="192.168.1.10", node_name="Switch1", node_role="switch", port="1", mac=mac),
            ClientSighting(node_id="pve-node", node_name="PVE", node_role="server", port="vmbr0", mac=mac, is_child=True),
        ]
        port_mac_sets = {"192.168.1.10": {"1": {mac}}}
        interlinks = {}

        edge = self.resolver.resolve_edge(mac, sightings, port_mac_sets, interlinks)
        self.assertTrue(edge.is_child)
        self.assertEqual(edge.node_id, "pve-node")

    def test_ap_precedence_over_switch(self):
        mac = "00:11:22:33:44:55"
        sightings = [
            ClientSighting(
                node_id="192.168.1.10",
                node_name="AccessSwitch",
                node_role="switch",
                port="5",
                mac=mac,
            ),
            ClientSighting(
                node_id="192.168.1.20",
                node_name="OfficeAP",
                node_role="access_point",
                node_device_type="access_point",
                port="ath0",
                mac=mac,
                ssid="CorpWiFi",
                signal_dbm=-60,
            ),
        ]
        port_mac_sets = {
            "192.168.1.10": {"5": {mac}},
            "192.168.1.20": {"ath0": {mac}},
        }
        interlinks = {}

        edge = self.resolver.resolve_edge(mac, sightings, port_mac_sets, interlinks)
        self.assertEqual(edge.node_id, "192.168.1.20")
        self.assertEqual(edge.port, "ath0")
        self.assertEqual(edge.ssid, "CorpWiFi")
        self.assertEqual(edge.signal_dbm, -60)

    def test_mac_density_and_interlink_penalty(self):
        mac = "AA:BB:CC:DD:EE:FF"
        sightings = [
            # Port 24 is an interlink / trunk with 10 MACs
            ClientSighting(node_id="192.168.1.1", node_name="CoreSwitch", node_role="switch", port="24", mac=mac),
            # Port 7 is an access port with only this 1 MAC
            ClientSighting(node_id="192.168.1.2", node_name="EdgeSwitch", node_role="switch", port="7", mac=mac),
        ]
        port_mac_sets = {
            "192.168.1.1": {"24": {mac, "11:22:33:44:55:66", "22:33:44:55:66:77"}},
            "192.168.1.2": {"7": {mac}},
        }
        interlinks = {"192.168.1.1": {"24"}}

        edge = self.resolver.resolve_edge(mac, sightings, port_mac_sets, interlinks)
        self.assertEqual(edge.node_id, "192.168.1.2")
        self.assertEqual(edge.port, "7")

    def test_router_gateway_subinterface_penalty(self):
        mac = "AA:BB:CC:DD:EE:01"
        sightings = [
            # Router sees it on a vlan subinterface
            ClientSighting(node_id="192.168.1.254", node_name="Gateway", node_role="router", port="vlan.10", mac=mac),
            # Switch sees it on an access port
            ClientSighting(node_id="192.168.1.5", node_name="SwitchA", node_role="switch", port="2", mac=mac),
        ]
        port_mac_sets = {
            "192.168.1.254": {"vlan.10": {mac}},
            "192.168.1.5": {"2": {mac}},
        }
        interlinks = {}

        edge = self.resolver.resolve_edge(mac, sightings, port_mac_sets, interlinks)
        self.assertEqual(edge.node_id, "192.168.1.5")
        self.assertEqual(edge.port, "2")

    def test_incumbent_stability_bonus_prevents_port_bounce(self):
        mac = "12:34:56:78:9A:BC"
        # Sighting 1: Incumbent port 8 (which has 2 MACs on it, base score 300)
        # Sighting 2: Ghost/transient port 2 on same switch (has 1 MAC on it, base score 500)
        sightings = [
            ClientSighting(node_id="192.168.1.10", node_name="SwitchA", port="8", mac=mac),
            ClientSighting(node_id="192.168.1.10", node_name="SwitchA", port="2", mac=mac),
        ]
        port_mac_sets = {
            "192.168.1.10": {
                "8": {mac, "00:11:22:33:44:55"},  # 2 MACs -> score 300
                "2": {mac},                        # 1 MAC -> score 500
            }
        }
        interlinks = {}

        # 1. With incumbent_location set to Port 8:
        # Port 8 gets 300 + 350 = 650, beating transient Port 2's 500!
        edge = self.resolver.resolve_edge(
            mac,
            sightings,
            port_mac_sets,
            interlinks,
            incumbent_location=("192.168.1.10", "8"),
        )
        self.assertEqual(edge.port, "8", "Incumbent port must resist transient port bounce")

        # 2. When device genuinely moves to a Wi-Fi AP: AP score (1000+) overrides incumbent
        ap_sighting = ClientSighting(
            node_id="192.168.1.50",
            node_name="OfficeAP",
            node_role="access_point",
            port="ath0",
            mac=mac,
            ssid="OfficeWiFi",
            signal_dbm=-55,
        )
        roam_edge = self.resolver.resolve_edge(
            mac,
            sightings + [ap_sighting],
            port_mac_sets,
            interlinks,
            incumbent_location=("192.168.1.10", "8"),
        )
        self.assertEqual(roam_edge.node_id, "192.168.1.50")
        self.assertEqual(roam_edge.port, "ath0")


if __name__ == "__main__":
    unittest.main()
