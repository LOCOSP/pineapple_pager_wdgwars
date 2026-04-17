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


def fake_snap() -> GpsSnapshot:
    return GpsSnapshot(
        fix_3d=True, fix_quality=1, lat=52.4001, lon=16.9221,
        alt_m=87.0, accuracy_m=8.0, sats=9, utc_iso="",
        last_update=0.0, device="/dev/ttyACM0",
    )


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
            sess = Session(Path(td), max_file_mb=10, dedup_ttl_s=60)
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


if __name__ == "__main__":
    unittest.main()
