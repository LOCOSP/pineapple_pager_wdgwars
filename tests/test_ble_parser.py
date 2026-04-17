import unittest
from pathlib import Path

from . import conftest_path  # noqa: F401
from scanners.ble import parse_bluetoothctl_lines


FIXTURE = Path(__file__).parent / "fixtures" / "bluetoothctl_sample.txt"


class TestBleParser(unittest.TestCase):
    def test_extracts_devices_old_and_new_formats(self):
        lines = FIXTURE.read_text().splitlines()
        obs = parse_bluetoothctl_lines(lines, now=1700000000.0)
        macs = {o.mac for o in obs}
        self.assertEqual(macs, {
            "aa:bb:cc:dd:ee:01",
            "11:22:33:44:55:66",
            "99:88:77:66:55:44",
            "28:56:5a:8a:4e:70",
        })
        by_mac = {o.mac: o for o in obs}
        self.assertEqual(by_mac["aa:bb:cc:dd:ee:01"].name, "PixelBuds")
        self.assertEqual(by_mac["aa:bb:cc:dd:ee:01"].rssi, -54)
        self.assertEqual(by_mac["11:22:33:44:55:66"].name, "MiBand5")
        # latest RSSI wins for repeated samples
        self.assertEqual(by_mac["99:88:77:66:55:44"].rssi, -63)
        # bluez 5.72 hex+decimal format
        self.assertEqual(by_mac["28:56:5a:8a:4e:70"].rssi, -79)
        self.assertEqual(by_mac["28:56:5a:8a:4e:70"].name, "BRAVIA 4K GB")


if __name__ == "__main__":
    unittest.main()
