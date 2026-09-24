import os
import json
import unittest
import tempfile
from switch_dashboard.storage.database import Database
from switch_dashboard.storage.repositories.metric_repo import MetricRepository
from switch_dashboard.storage.migration import migrate_legacy_data


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.test_dir.name, "test_dash.db")
        self.db = Database(self.db_path)
        self.db.init_db()
        self.metric_repo = MetricRepository(self.db)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_legacy_data_migration(self):
        # Create legacy counters file
        counters_file = os.path.join(self.test_dir.name, "counters.json")
        sample_counters = {
            "192.168.1.100:1": {
                "tx": 1000,
                "rx": 2000,
                "cum_tx": 10000,
                "cum_rx": 20000,
                "ts": 1234567.0
            }
        }
        with open(counters_file, "w") as f:
            json.dump(sample_counters, f)

        # Monkeypatch config path
        import switch_dashboard.storage.migration as mig
        orig_counters_path = mig.COUNTERS_PATH
        mig.COUNTERS_PATH = counters_file

        try:
            stats = migrate_legacy_data(self.metric_repo, self.db)
            self.assertEqual(stats["counters_migrated"], 1)

            # Check that file was renamed to .bak
            self.assertFalse(os.path.exists(counters_file))
            self.assertTrue(os.path.exists(f"{counters_file}.bak"))

            # Check that counters exist in db
            loaded = self.metric_repo.load_counters()
            self.assertIn("192.168.1.100:1", loaded)
            self.assertEqual(loaded["192.168.1.100:1"]["cum_tx"], 10000)
        finally:
            mig.COUNTERS_PATH = orig_counters_path


if __name__ == "__main__":
    unittest.main()
