import unittest

from . import conftest_path  # noqa: F401
from storage.dedup import TtlDedup


class TestTtlDedup(unittest.TestCase):
    def test_first_write_passes(self):
        d = TtlDedup(ttl_s=60)
        self.assertTrue(d.should_write("wifi", "aa:bb", now=1000.0))

    def test_repeat_within_ttl_blocked(self):
        d = TtlDedup(ttl_s=60)
        d.should_write("wifi", "aa:bb", now=1000.0)
        self.assertFalse(d.should_write("wifi", "aa:bb", now=1030.0))

    def test_after_ttl_passes_again(self):
        d = TtlDedup(ttl_s=60)
        d.should_write("wifi", "aa:bb", now=1000.0)
        self.assertTrue(d.should_write("wifi", "aa:bb", now=1061.0))

    def test_case_insensitive(self):
        d = TtlDedup(ttl_s=60)
        self.assertTrue(d.should_write("ble", "AA:BB:CC:DD:EE:01", now=0))
        self.assertFalse(d.should_write("ble", "aa:bb:cc:dd:ee:01", now=10))

    def test_kinds_independent(self):
        d = TtlDedup(ttl_s=60)
        self.assertTrue(d.should_write("wifi", "x", now=0))
        self.assertTrue(d.should_write("ble", "x", now=0))


if __name__ == "__main__":
    unittest.main()
