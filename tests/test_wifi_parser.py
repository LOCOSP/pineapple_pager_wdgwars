import unittest
from pathlib import Path

from . import conftest_path  # noqa: F401  ensures sys.path is set
from scanners.wifi import parse_iw_scan


FIXTURE = Path(__file__).parent / "fixtures" / "iw_scan_sample.txt"


class TestWifiParser(unittest.TestCase):
    def setUp(self):
        self.text = FIXTURE.read_text()
        self.obs = parse_iw_scan(self.text, ts=0.0)

    def test_count(self):
        self.assertEqual(len(self.obs), 4)

    def test_wpa2_psk_ccmp(self):
        wpa2 = self.obs[0]
        self.assertEqual(wpa2.bssid, "aa:bb:cc:11:22:33")
        self.assertEqual(wpa2.ssid, "HomeNet")
        self.assertEqual(wpa2.channel, 6)
        self.assertEqual(wpa2.frequency, 2437)
        self.assertEqual(wpa2.rssi, -52)
        self.assertIn("[WPA2-PSK", wpa2.auth)
        self.assertTrue(wpa2.auth.endswith("[ESS]"))

    def test_open_network(self):
        op = self.obs[1]
        self.assertEqual(op.bssid, "de:ad:be:ef:00:01")
        self.assertEqual(op.ssid, "OpenAP")
        self.assertEqual(op.auth, "[ESS]")
        self.assertEqual(op.channel, 36)

    def test_wep_falls_back(self):
        wpa1 = self.obs[2]
        self.assertEqual(wpa1.ssid, "LegacyWPA")
        self.assertIn("[WPA-PSK", wpa1.auth)

    def test_wpa3_sae(self):
        sae = self.obs[3]
        self.assertEqual(sae.ssid, "SAE-Net")
        self.assertIn("[WPA3-SAE", sae.auth)
        self.assertEqual(sae.channel, 100)


if __name__ == "__main__":
    unittest.main()
