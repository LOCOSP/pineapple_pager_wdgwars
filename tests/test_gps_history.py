import unittest

from . import conftest_path  # noqa: F401
from scanners.gps import GpsReader, GpsState, _accuracy_from


def state_with_track(points):
    """points: list of (t, lat, lon, has_fix)"""
    st = GpsState()
    for t, lat, lon, fix in points:
        st.lat, st.lon, st.alt_m, st.accuracy_m = lat, lon, 100.0, 5.0
        st.fix_3d = fix
        st.last_update = t
        st._record(t, lat, lon, 100.0, 5.0, fix)
    return st


class TestPositionHistory(unittest.TestCase):
    """`iw scan` hands us sightings up to seconds old. Geo-tagging them with
    the current position is ~400 m of error at 50 km/h."""

    def setUp(self):
        # Straight run north, 1 Hz, ~11 m per second.
        self.st = state_with_track(
            [(1000.0 + i, 52.4000 + i * 0.0001, 16.9000, True)
             for i in range(10)]
        )

    def test_exact_sample_is_returned(self):
        snap = self.st.at(1003.0)
        self.assertTrue(snap.fix_3d)
        self.assertAlmostEqual(snap.lat, 52.4003, places=6)

    def test_interpolates_between_samples(self):
        snap = self.st.at(1003.5)
        self.assertTrue(snap.interpolated)
        self.assertAlmostEqual(snap.lat, 52.40035, places=6)

    def test_back_dated_observation_gets_the_old_position(self):
        now_snap = self.st.at(1009.0)
        old_snap = self.st.at(1002.0)
        self.assertNotAlmostEqual(now_snap.lat, old_snap.lat, places=5)
        self.assertAlmostEqual(old_snap.lat, 52.4002, places=6)

    def test_future_timestamp_clamps_to_newest(self):
        snap = self.st.at(1009.5)
        self.assertAlmostEqual(snap.lat, 52.4009, places=6)
        self.assertTrue(snap.fix_3d)

    def test_far_future_loses_the_fix(self):
        snap = self.st.at(1100.0, max_gap_s=6.0)
        self.assertFalse(snap.fix_3d)

    def test_far_past_loses_the_fix(self):
        snap = self.st.at(900.0, max_gap_s=6.0)
        self.assertFalse(snap.fix_3d)

    def test_no_history_and_stale_live_fix_loses_the_fix(self):
        st = GpsState()
        st.fix_3d = True
        st.last_update = 0.0        # never updated
        self.assertFalse(st.at(1000.0).fix_3d)


class TestFixDropouts(unittest.TestCase):
    def test_no_interpolation_across_a_dropout(self):
        """A tunnel is not a straight line between the last good fix and the
        first one after it — never invent a position across the gap."""
        st = state_with_track([
            (1000.0, 52.4000, 16.9000, True),
            (1001.0, 52.4000, 16.9000, False),   # fix lost, lat/lon frozen
            (1002.0, 52.4050, 16.9000, True),    # reacquired 550 m away
        ])
        snap = st.at(1001.5)
        self.assertFalse(snap.interpolated)

    def test_dropout_sample_is_not_writable(self):
        st = state_with_track([
            (1000.0, 52.4000, 16.9000, True),
            (1001.0, 52.4000, 16.9000, False),
        ])
        self.assertFalse(st.at(1001.0).fix_3d)

    def test_snapshot_keeps_last_known_position_for_display(self):
        st = state_with_track([
            (1000.0, 52.4000, 16.9000, True),
            (1001.0, 52.4000, 16.9000, False),
        ])
        snap = st.snapshot()
        self.assertEqual(snap.lat, 52.4000)     # still shown on the HUD
        self.assertFalse(snap.fix_3d)           # but not writable


class TestHistoryBounds(unittest.TestCase):
    def test_history_is_trimmed(self):
        st = GpsState()
        for i in range(2000):
            st._record(float(i), 52.0, 16.0, 100.0, 5.0, True)
        self.assertLessEqual(len(st._hist_t), 300)
        self.assertEqual(len(st._hist_t), len(st._hist_p))

    def test_out_of_order_timestamps_stay_sorted(self):
        st = GpsState()
        st._record(100.0, 52.0, 16.0, 0.0, 5.0, True)
        st._record(50.0, 52.1, 16.0, 0.0, 5.0, True)   # gpsd replay
        self.assertEqual(st._hist_t, sorted(st._hist_t))


class TestAccuracy(unittest.TestCase):
    def test_prefers_eph(self):
        self.assertEqual(_accuracy_from({"eph": 7.5}, 3), 7.5)

    def test_falls_back_to_epx_epy(self):
        self.assertAlmostEqual(_accuracy_from({"epx": 3.0, "epy": 4.0}, 3), 5.0)

    def test_never_returns_zero(self):
        # WiGLE downweights rows claiming a zero-metre error.
        self.assertGreater(_accuracy_from({}, 3), 0.0)
        self.assertGreater(_accuracy_from({"eph": 0.0}, 2), 0.0)


class TestTpvApply(unittest.TestCase):
    def test_fix_loss_records_a_dropout_marker(self):
        r = GpsReader([], min_sats=4)
        r.state.sats = 9
        r._apply({"class": "TPV", "mode": 3, "lat": 52.4, "lon": 16.9,
                  "alt": 100.0, "eph": 5.0})
        self.assertTrue(r.state.fix_3d)
        r._apply({"class": "TPV", "mode": 1})
        self.assertFalse(r.state.fix_3d)
        # lat/lon preserved for the HUD, but the history knows it is unusable
        self.assertEqual(r.state.lat, 52.4)
        self.assertFalse(r.state._hist_p[-1][4])

    def test_speed_is_captured(self):
        r = GpsReader([], min_sats=1)
        r.state.sats = 9
        r._apply({"class": "TPV", "mode": 3, "lat": 1.0, "lon": 2.0,
                  "speed": 13.9})
        self.assertAlmostEqual(r.state.speed_mps, 13.9)


if __name__ == "__main__":
    unittest.main()
