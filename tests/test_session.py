import tempfile
import unittest
from pathlib import Path

from . import conftest_path  # noqa: F401
from scanners.gps import GpsSnapshot
from scanners.wifi import WifiObs
from scanners.ble import BleObs
from storage.session import (
    Session, WIGLE_HEADER, COLUMNS, list_pending, list_all, mark_uploaded,
)


def fake_snap(lat: float = 52.4001, lon: float = 16.9221,
              fix_3d: bool = True, accuracy_m: float = 8.0) -> GpsSnapshot:
    return GpsSnapshot(
        fix_3d=fix_3d, fix_quality=3 if fix_3d else 0, lat=lat, lon=lon,
        alt_m=87.0, accuracy_m=accuracy_m, sats=9, utc_iso="",
        last_update=0.0, device="/dev/ttyACM0",
    )


def wifi_obs(bssid="aa:bb:cc:dd:ee:ff", rssi=-58, first_seen=1000.0):
    return WifiObs(bssid=bssid, ssid="Net", channel=6, frequency=2437,
                   rssi=rssi, auth="[WPA2-PSK-CCMP][ESS]",
                   first_seen=first_seen)


class TestSession(unittest.TestCase):
    def test_writes_wigle_header(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td), max_file_mb=10)
            sess.close()
            csv = Path(sess.stats.files[0])
            lines = csv.read_text().splitlines()
            self.assertEqual(lines[0], WIGLE_HEADER)
            self.assertEqual(lines[1], COLUMNS)
            # Hak5 Pager Op badge requires one of these triggers in the header
            # (case-insensitive). See docs/hak5-pager-badge-integration.md.
            lower = lines[0].lower()
            self.assertTrue(any(t in lower for t in (
                "hak5 pager", "hak5pager", "pineapple pager", "hak5_pager"
            )), f"WIGLE_HEADER missing badge trigger keyword: {lines[0]!r}")

    def test_wifi_and_ble_rows_and_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td), max_file_mb=10, refresh_ttl_s=60)
            snap = fake_snap()
            wifi = WifiObs(bssid="aa:bb:cc:dd:ee:ff", ssid="Net,1\"q",
                           channel=6, frequency=2437, rssi=-58,
                           auth="[WPA2-PSK-CCMP][ESS]", first_seen=1000.0)
            ble = BleObs(mac="11:22:33:44:55:66", name="Watch",
                         rssi=-71, first_seen=1000.5)
            self.assertTrue(sess.add_wifi(wifi, snap))
            self.assertFalse(sess.add_wifi(wifi, snap))  # dedup blocks immediately
            self.assertTrue(sess.add_ble(ble, snap))
            sess.close()

            csv = Path(sess.stats.files[0])
            lines = csv.read_text().splitlines()
            self.assertEqual(len(lines), 4)  # header + columns + 2 rows
            wifi_row = lines[2].split(",")
            self.assertEqual(wifi_row[0], "aa:bb:cc:dd:ee:ff")
            # SSID should be quoted because it contains comma + quote
            self.assertTrue(lines[2].count('"') >= 2)
            self.assertEqual(wifi_row[-1], "WIFI")
            ble_row = lines[3].split(",")
            self.assertEqual(ble_row[0], "11:22:33:44:55:66")
            self.assertEqual(ble_row[2], "[BLE]")
            self.assertEqual(ble_row[-1], "BLE")

    def test_pending_and_uploaded_listing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sess = Session(root, max_file_mb=10)
            sess.close()
            csv = Path(sess.stats.files[0])
            self.assertEqual([p.name for p in list_pending(root)], [csv.name])

            mark_uploaded(csv, '{"ok":true,"merged_samples":3}')
            self.assertEqual(list_pending(root), [])
            statuses = [s for _, s in list_all(root)]
            self.assertEqual(statuses, ["ok"])


class TestGpsFixGate(unittest.TestCase):
    """Without a fix, `GpsState` keeps the last known lat/lon for the HUD.
    Writing those pins every AP in a tunnel to one coordinate."""

    def test_rows_are_refused_without_a_fix(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td))
            self.assertFalse(sess.add_wifi(wifi_obs(), fake_snap(fix_3d=False)))
            self.assertEqual(sess.stats.rows_written, 0)
            self.assertEqual(sess.stats.skipped_no_fix, 1)
            sess.close()

    def test_ble_rows_are_refused_too(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td))
            ble = BleObs(mac="11:22:33:44:55:66", name="W", rssi=-70,
                         first_seen=1000.0)
            self.assertFalse(sess.add_ble(ble, fake_snap(fix_3d=False)))
            self.assertEqual(sess.stats.skipped_no_fix, 1)
            sess.close()

    def test_gate_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td), require_fix=False)
            self.assertTrue(sess.add_wifi(wifi_obs(), fake_snap(fix_3d=False)))
            sess.close()

    def test_fix_returning_resumes_writing(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td))
            sess.add_wifi(wifi_obs(), fake_snap(fix_3d=False))
            self.assertTrue(sess.add_wifi(wifi_obs(), fake_snap()))
            sess.close()


class TestGeoDedupIntegration(unittest.TestCase):
    def test_parked_repeat_is_suppressed(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td), min_move_m=30, refresh_ttl_s=300)
            snap = fake_snap()
            self.assertTrue(sess.add_wifi(wifi_obs(first_seen=1000.0), snap))
            for t in (1030.0, 1060.0, 1120.0, 1200.0):
                self.assertFalse(sess.add_wifi(wifi_obs(first_seen=t), snap))
            self.assertEqual(sess.stats.rows_written, 1)
            self.assertEqual(sess.stats.skipped_dedup, 4)
            sess.close()

    def test_moving_produces_more_rows(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td), min_move_m=30)
            for i in range(10):
                sess.add_wifi(wifi_obs(first_seen=1000.0 + i),
                              fake_snap(lat=52.4001 + i * 0.0004))
            self.assertGreaterEqual(sess.stats.rows_written, 5)
            sess.close()


class TestRowContent(unittest.TestCase):
    def _row(self, sess) -> list[str]:
        sess.close()
        csv = Path(sess.stats.files[0])
        return csv.read_text().splitlines()[2].split(",")

    def test_timestamp_comes_from_the_observation_not_write_time(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td))
            # 2001-09-09 01:46:40 UTC
            sess.add_wifi(wifi_obs(first_seen=1000000000.0), fake_snap())
            self.assertEqual(self._row(sess)[3], "2001-09-09 01:46:40")

    def test_accuracy_never_written_as_zero(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td))
            sess.add_wifi(wifi_obs(), fake_snap(accuracy_m=0.0))
            self.assertNotEqual(float(self._row(sess)[10]), 0.0)


class TestWriterMechanics(unittest.TestCase):
    def test_rows_are_visible_without_closing(self):
        """An SSH `wc -l` mid-drive must see progress."""
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td), flush_interval_s=0.0)
            sess.add_wifi(wifi_obs(), fake_snap())
            csv = Path(sess.stats.files[0])
            self.assertEqual(len(csv.read_text().splitlines()), 3)
            sess.close()

    def test_explicit_flush(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td), flush_interval_s=3600.0)
            sess.add_wifi(wifi_obs(), fake_snap())
            sess.flush()
            csv = Path(sess.stats.files[0])
            self.assertEqual(len(csv.read_text().splitlines()), 3)
            sess.close()

    def test_byte_counter_matches_real_size(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td))
            for i in range(20):
                sess.add_wifi(wifi_obs(bssid=f"aa:bb:cc:00:00:{i:02x}"),
                              fake_snap())
            counted = sess._bytes
            sess.close()
            self.assertEqual(Path(sess.stats.files[0]).stat().st_size, counted)

    def test_rotation_opens_a_second_file_with_headers(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Session(Path(td), max_file_mb=0.001)   # ~1 KB
            for i in range(60):
                sess.add_wifi(wifi_obs(bssid=f"aa:bb:cc:00:01:{i:02x}"),
                              fake_snap())
            sess.close()
            self.assertGreater(len(sess.stats.files), 1)
            for path in sess.stats.files:
                lines = Path(path).read_text().splitlines()
                self.assertEqual(lines[0], WIGLE_HEADER)
                self.assertEqual(lines[1], COLUMNS)

    def test_all_rotated_files_are_listed_as_pending(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sess = Session(root, max_file_mb=0.001)
            for i in range(60):
                sess.add_wifi(wifi_obs(bssid=f"aa:bb:cc:00:02:{i:02x}"),
                              fake_snap())
            sess.close()
            self.assertEqual(len(list_pending(root)), len(sess.stats.files))


if __name__ == "__main__":
    unittest.main()
