import unittest
import app
import scraper
import network_scanner
import scanner_db


class TestFacades(unittest.TestCase):
    def test_app_facade_symbols(self):
        self.assertIsNotNone(app.app)
        self.assertIsNotNone(app.unify_speed)
        self.assertIsNotNone(app.format_bps)
        self.assertIsNotNone(app.normalize_mac)
        self.assertIsNotNone(app.is_ignored_mac)
        self.assertEqual(app.VERSION, "2.0.0")

    def test_scraper_facade_symbols(self):
        self.assertIsNotNone(scraper.HCSwitchScraper)
        self.assertIsNotNone(scraper.OVSScraper)
        self.assertIsNotNone(scraper.FritzBoxScraper)
        self.assertIsNotNone(scraper.get_switch_lock)
        self.assertIsNotNone(scraper.scrape_switch)

    def test_network_scanner_facade_symbols(self):
        self.assertIsNotNone(network_scanner.parse_port_range)
        self.assertIsNotNone(network_scanner.scan_port)
        self.assertIsNotNone(network_scanner.scan_ports_threaded)
        self.assertIsNotNone(network_scanner.scan_network)
        self.assertIsNotNone(network_scanner.is_host_reachable_by_ping)
        self.assertIsNotNone(network_scanner.get_vendor)

    def test_scanner_db_facade_symbols(self):
        self.assertIsNotNone(scanner_db.get_db_connection)
        self.assertIsNotNone(scanner_db.init_db)
        self.assertIsNotNone(scanner_db.load_state_from_db)
        self.assertIsNotNone(scanner_db.update_db_and_get_status)
        self.assertIsNotNone(scanner_db.get_history_data)
        self.assertIsNotNone(scanner_db.delete_host_history)
        self.assertIsNotNone(scanner_db.delete_all_history)


if __name__ == "__main__":
    unittest.main()
