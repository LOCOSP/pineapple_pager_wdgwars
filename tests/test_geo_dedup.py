import unittest

from . import conftest_path  # noqa: F401
from storage.dedup import GeoDedup, distance_m


# Poznan-ish. 0.001 deg latitude is ~111 m; 0.0001 is ~11 m.
LAT, LON = 52.4064, 16.9252


class TestDistance(unittest.TestCase):
    def test_latitude_degree_is_about_111km(self):
        self.assertAlmostEqual(distance_m(0.0, 0.0, 1.0, 0.0), 111320.0, delta=1)

    def test_longitude_shrinks_with_latitude(self):
        at_equator = distance_m(0.0, 0.0, 0.0, 1.0)
        at_52 = distance_m(52.0, 0.0, 52.0, 1.0)
        self.assertLess(at_52, at_equator * 0.7)

    def test_short_hop(self):
        d = distance_m(LAT, LON, LAT + 0.0009, LON)
        self.assertAlmostEqual(d, 100.0, delta=2.0)


class TestGeoDedup(unittest.TestCase):
    def test_first_sighting_always_writes(self):
        d = GeoDedup()
        self.assertTrue(d.should_write("wifi", "aa:bb", 1000.0, LAT, LON, -60))

    def test_standing_still_is_suppressed(self):
        """The bug report's symptom: parked, the CSV grew by ~40 rows/min."""
        d = GeoDedup(ttl_s=300, min_move_m=30)
        d.should_write("wifi", "aa:bb", 1000.0, LAT, LON, -60)
        for t in range(1010, 1290, 10):
            self.assertFalse(
                d.should_write("wifi", "aa:bb", float(t), LAT, LON, -60),
                f"unexpected write at t={t} without moving")

    def test_moving_far_enough_writes(self):
        d = GeoDedup(min_move_m=30)
        d.should_write("wifi", "aa:bb", 1000.0, LAT, LON, -60)
        # ~44 m north
        self.assertTrue(d.should_write("wifi", "aa:bb", 1001.0,
                                       LAT + 0.0004, LON, -60))

    def test_small_movement_is_suppressed(self):
        d = GeoDedup(min_move_m=30)
        d.should_write("wifi", "aa:bb", 1000.0, LAT, LON, -60)
        # ~11 m north — inside GPS noise, adds nothing
        self.assertFalse(d.should_write("wifi", "aa:bb", 1001.0,
                                        LAT + 0.0001, LON, -60))

    def test_stronger_signal_writes_even_without_moving(self):
        d = GeoDedup(min_move_m=30, rssi_delta_db=6)
        d.should_write("wifi", "aa:bb", 1000.0, LAT, LON, -70)
        self.assertTrue(d.should_write("wifi", "aa:bb", 1001.0, LAT, LON, -64))

    def test_marginally_stronger_signal_is_suppressed(self):
        d = GeoDedup(min_move_m=30, rssi_delta_db=6)
        d.should_write("wifi", "aa:bb", 1000.0, LAT, LON, -70)
        self.assertFalse(d.should_write("wifi", "aa:bb", 1001.0, LAT, LON, -66))

    def test_ttl_still_refreshes_eventually(self):
        d = GeoDedup(ttl_s=300, min_move_m=30)
        d.should_write("wifi", "aa:bb", 1000.0, LAT, LON, -60)
        self.assertTrue(d.should_write("wifi", "aa:bb", 1300.0, LAT, LON, -60))

    def test_case_insensitive_and_kind_scoped(self):
        d = GeoDedup()
        self.assertTrue(d.should_write("ble", "AA:BB:CC:DD:EE:01", 0.0, LAT, LON, -50))
        self.assertFalse(d.should_write("ble", "aa:bb:cc:dd:ee:01", 1.0, LAT, LON, -50))
        self.assertTrue(d.should_write("wifi", "aa:bb:cc:dd:ee:01", 1.0, LAT, LON, -50))

    def test_driving_past_yields_several_rows(self):
        """Driving, the old 60 s TTL gave one row per AP. Movement gating
        gives one per `min_move_m`, which is what trilateration needs."""
        d = GeoDedup(ttl_s=300, min_move_m=30)
        writes = 0
        for i in range(20):                     # 20 ticks, ~22 m apart
            if d.should_write("wifi", "aa:bb", 1000.0 + i,
                              LAT + i * 0.0002, LON, -60):
                writes += 1
        self.assertGreaterEqual(writes, 5)
        self.assertLessEqual(writes, 20)

    def test_prune_bounds_the_table(self):
        d = GeoDedup(max_entries=100, prune_after_s=10.0)
        for i in range(120):
            d.should_write("wifi", f"aa:bb:{i:04x}", 0.0, LAT, LON, -60)
        # Everything is older than prune_after_s by now.
        d.should_write("wifi", "ff:ff:ff", 1000.0, LAT, LON, -60)
        self.assertLessEqual(len(d), 100)


if __name__ == "__main__":
    unittest.main()
