import unittest
from pathlib import Path

from . import conftest_path  # noqa: F401
from scanners.gps import parse_nmea, _utc_to_iso


FIXTURE = Path(__file__).parent / "fixtures" / "nmea_sample.txt"


class TestGpsParser(unittest.TestCase):
    def test_gga_with_fix(self):
        line = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
        v = parse_nmea(line)
        self.assertIsNotNone(v)
        self.assertEqual(v["sentence"], "GGA")
        self.assertAlmostEqual(v["lat"], 48.1173, places=4)
        self.assertAlmostEqual(v["lon"], 11.5167, places=4)
        self.assertEqual(v["fix_quality"], 1)
        self.assertEqual(v["sats"], 8)
        self.assertAlmostEqual(v["alt_m"], 545.4)

    def test_rmc_active(self):
        line = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
        v = parse_nmea(line)
        self.assertIsNotNone(v)
        self.assertTrue(v["active"])
        self.assertEqual(v["date"], "230394")

    def test_rmc_void(self):
        line = "$GPRMC,235959,V,,,,,,,010100,,*30"
        v = parse_nmea(line)
        self.assertIsNotNone(v)
        self.assertFalse(v["active"])

    def test_bad_checksum_rejected(self):
        v = parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*00")
        self.assertIsNone(v)

    def test_unknown_sentence_ignored(self):
        v = parse_nmea("$GPGSV,3,1,11,01,40,083,46,02,17,308,41*71")
        self.assertIsNone(v)

    def test_utc_iso(self):
        self.assertEqual(_utc_to_iso("230394", "123519"), "2094-03-23 12:35:19")
        self.assertEqual(_utc_to_iso("010126", "000000"), "2026-01-01 00:00:00")

    def test_fixture_yields_two_valid(self):
        valid = [parse_nmea(l) for l in FIXTURE.read_text().splitlines()]
        valid = [v for v in valid if v is not None]
        # GGA(fix), GGA(no fix), RMC(void)
        self.assertGreaterEqual(len(valid), 3)


if __name__ == "__main__":
    unittest.main()
