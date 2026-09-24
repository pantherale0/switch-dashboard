import unittest
from switch_dashboard.services.clients.hasher import compute_footprint_hash


class TestClientHasher(unittest.TestCase):
    def test_empty_footprint(self):
        self.assertEqual(compute_footprint_hash({}), "")

    def test_order_independence(self):
        f1 = {"sw1": "1", "sw2": "2", "ap1": "ath0"}
        f2 = {"ap1": "ath0", "sw1": "1", "sw2": "2"}
        f3 = {"sw2": "2", "ap1": "ath0", "sw1": "1"}

        h1 = compute_footprint_hash(f1)
        h2 = compute_footprint_hash(f2)
        h3 = compute_footprint_hash(f3)

        self.assertEqual(h1, h2)
        self.assertEqual(h2, h3)
        self.assertEqual(len(h1), 64)

    def test_value_sensitivity(self):
        f1 = {"sw1": "1", "sw2": "2"}
        f2 = {"sw1": "1", "sw2": "3"}
        self.assertNotEqual(compute_footprint_hash(f1), compute_footprint_hash(f2))

    def test_key_sensitivity(self):
        f1 = {"sw1": "1", "sw2": "2"}
        f2 = {"sw1": "1", "sw3": "2"}
        self.assertNotEqual(compute_footprint_hash(f1), compute_footprint_hash(f2))


if __name__ == "__main__":
    unittest.main()
