import unittest
from pathlib import Path

from . import conftest_path  # noqa: F401  ensures sys.path is set
from scanners.wifi import (
    BANDS, DEFAULT_PLAN, WifiScanner, _capability_flags, _decode_ssid,
    _freq_to_channel, parse_iw_scan,
)


FIXTURE = Path(__file__).parent / "fixtures" / "iw_scan_sample.txt"
EXTENDED = Path(__file__).parent / "fixtures" / "iw_scan_extended.txt"
REAL = Path(__file__).parent / "fixtures" / "iw_scan_pager_iw69.txt"


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


class TestLastSeenBackdating(unittest.TestCase):
    """`iw scan` returns the kernel BSS cache, so entries carry an age."""

    def setUp(self):
        self.obs = {o.ssid: o for o in
                    parse_iw_scan(EXTENDED.read_text(), ts=1000.0)}

    def test_age_is_parsed(self):
        self.assertAlmostEqual(self.obs["CorpNet"].age_s, 0.25)
        self.assertAlmostEqual(self.obs["StaleFromCache"].age_s, 21.4)

    def test_first_seen_is_back_dated(self):
        # 250 ms before the scan completed, not "now".
        self.assertAlmostEqual(self.obs["CorpNet"].first_seen, 999.75)
        self.assertAlmostEqual(self.obs["StaleFromCache"].first_seen, 978.6)

    def test_missing_last_seen_is_age_zero(self):
        obs = parse_iw_scan(FIXTURE.read_text(), ts=500.0)
        self.assertEqual(obs[1].age_s, 0.0)
        self.assertEqual(obs[1].first_seen, 500.0)


class TestAuthEdgeCases(unittest.TestCase):
    def setUp(self):
        self.obs = {o.ssid: o for o in
                    parse_iw_scan(EXTENDED.read_text(), ts=1000.0)}

    def test_enterprise_not_labelled_psk(self):
        auth = self.obs["CorpNet"].auth
        self.assertIn("[WPA2-EAP-CCMP]", auth)
        self.assertNotIn("PSK", auth)

    def test_transition_mode_keeps_both(self):
        auth = self.obs["SixGig"].auth
        self.assertIn("[WPA3-SAE-CCMP]", auth)
        self.assertIn("[WPA2-PSK-CCMP]", auth)

    def test_owe(self):
        auth = self.obs["OweOpen"].auth
        self.assertIn("[WPA3-OWE-CCMP]", auth)

    def test_wps_detected(self):
        auth = self.obs["Dom Kowalskichł"].auth
        self.assertIn("[WPS]", auth)

    def test_ibss_bss_type(self):
        auth = self.obs["AdHocNet"].auth
        self.assertTrue(auth.endswith("[IBSS]"))

    def test_mixed_wpa_wpa2(self):
        hidden = [o for o in self.obs.values() if o.ssid == ""][0]
        self.assertIn("[WPA-PSK-", hidden.auth)
        self.assertIn("[WPA2-PSK-", hidden.auth)


class TestSsidDecoding(unittest.TestCase):
    def test_utf8_escape_is_decoded(self):
        self.assertEqual(_decode_ssid(r"Dom Kowalskich\xc5\x82"),
                         "Dom Kowalskichł")

    def test_hidden_ssid_collapses_to_empty(self):
        self.assertEqual(_decode_ssid(r"\x00\x00\x00"), "")

    def test_plain_ascii_untouched(self):
        self.assertEqual(_decode_ssid("PlainNet"), "PlainNet")

    def test_escaped_backslash(self):
        self.assertEqual(_decode_ssid(r"a\\b"), "a\\b")


class TestFreqToChannel(unittest.TestCase):
    def test_24ghz(self):
        self.assertEqual(_freq_to_channel(2412), 1)
        self.assertEqual(_freq_to_channel(2437), 6)
        self.assertEqual(_freq_to_channel(2472), 13)
        self.assertEqual(_freq_to_channel(2484), 14)

    def test_5ghz_edges_the_old_range_missed(self):
        self.assertEqual(_freq_to_channel(5160), 32)     # was 0 before
        self.assertEqual(_freq_to_channel(5865), 173)    # was 0 before
        self.assertEqual(_freq_to_channel(5180), 36)
        self.assertEqual(_freq_to_channel(5825), 165)

    def test_6ghz(self):
        self.assertEqual(_freq_to_channel(5955), 1)
        self.assertEqual(_freq_to_channel(5935), 2)
        self.assertEqual(_freq_to_channel(7075), 225)

    def test_unknown(self):
        self.assertEqual(_freq_to_channel(0), 0)


class TestBandPlan(unittest.TestCase):
    def test_default_plan_entries_are_known_bands(self):
        for band in DEFAULT_PLAN:
            self.assertIn(band, BANDS)

    def test_24ghz_gets_half_the_slots(self):
        # Most of what a wardrive logs lives on 2.4 GHz, and its pass is the
        # cheapest, so it should come around often.
        self.assertGreaterEqual(DEFAULT_PLAN.count("2g"), len(DEFAULT_PLAN) // 2)

    def test_rotation_skips_unsupported_frequencies(self):
        sc = WifiScanner("wlan0", plan=["2g", "6g_psc"])
        sc._supported = {2412, 2437, 2462}       # 2.4 GHz-only radio
        seen = [sc._next_band() for _ in range(4)]
        self.assertTrue(all(name == "2g" for name, _ in seen))
        self.assertEqual(seen[0][1], [2412, 2437, 2462])

    def test_unknown_band_names_are_dropped(self):
        sc = WifiScanner("wlan0", plan=["nonsense"])
        self.assertEqual(sc.plan, ["all"])

    def test_repeatedly_failing_band_is_retired(self):
        sc = WifiScanner("wlan0", plan=["2g", "6g_psc"])
        sc._band_fail["6g_psc"] = sc.BAND_FAIL_LIMIT
        seen = {sc._next_band()[0] for _ in range(6)}
        self.assertEqual(seen, {"2g"})

    def test_all_bands_retired_falls_back_to_a_full_sweep(self):
        sc = WifiScanner("wlan0", plan=["2g", "6g_psc"])
        for band in ("2g", "6g_psc"):
            sc._band_fail[band] = sc.BAND_FAIL_LIMIT
        self.assertEqual(sc._next_band(), ("all", []))

    def test_full_sweep_gets_the_long_timeout(self):
        sc = WifiScanner("wlan0", plan=["all"], full_sweep_timeout_s=30.0)
        self.assertEqual(sc._timeout_for([]), 30.0)
        # A banded pass must not inherit the tri-band budget.
        self.assertLess(sc._timeout_for([2412, 2437]), 30.0)


class TestStaleFiltering(unittest.TestCase):
    def test_cached_entries_are_dropped(self):
        sc = WifiScanner("wlan0")
        sc.last_scan_s = 1.5
        sc.max_age_s = None       # derive from the sweep duration
        sc._ingest(EXTENDED.read_text())
        names = {o.ssid for o in sc.drain()}
        self.assertNotIn("StaleFromCache", names)   # 21.4 s old
        self.assertIn("CorpNet", names)             # 0.25 s old
        self.assertEqual(sc.dropped_stale, 1)

    def test_explicit_max_age_keeps_everything(self):
        sc = WifiScanner("wlan0", max_age_s=60.0)
        sc._ingest(EXTENDED.read_text())
        self.assertEqual(len(sc.drain()), 7)
        self.assertEqual(sc.dropped_stale, 0)


class TestCapabilityFlags(unittest.TestCase):
    """iw 6.9 on the Pager prints `capability: ESS (0x0431)` and never the
    word `Privacy`, so the hex value has to be authoritative."""

    def test_hex_privacy_bit_beats_missing_word(self):
        privacy, ess = _capability_flags("ESS (0x0431)")
        self.assertTrue(privacy)
        self.assertTrue(ess)

    def test_hex_without_privacy(self):
        privacy, _ = _capability_flags("ESS (0x0001)")
        self.assertFalse(privacy)

    def test_hex_ibss_bit(self):
        _, ess = _capability_flags("IBSS (0x0012)")
        self.assertFalse(ess)

    def test_falls_back_to_words_when_no_hex(self):
        privacy, ess = _capability_flags("ESS Privacy ShortPreamble")
        self.assertTrue(privacy)
        self.assertTrue(ess)

    def test_word_fallback_ibss(self):
        _, ess = _capability_flags("IBSS Privacy")
        self.assertFalse(ess)

    def test_wep_survives_the_terse_iw_69_format(self):
        # Open+Privacy and no RSN/WPA element == WEP. Before the hex fix this
        # came out as a plain open network on this firmware.
        block = "BSS aa:bb:cc:dd:ee:ff(on wlan0)\n\tcapability: ESS (0x0431)\n\tSSID: OldRouter\n"
        obs = parse_iw_scan(block, ts=0.0)
        self.assertEqual(obs[0].auth, "[WEP][ESS]")


class TestRealDeviceCapture(unittest.TestCase):
    """Captured from a WiFi Pineapple Pager running OpenWrt 24.10.1 / iw 6.9.

    Guards three format differences that broke assumptions built against
    older iw: a second `last seen: <boottime>` line, `freq:` printed as a
    float, and the terse capability line.

    BSSIDs and SSIDs are rewritten to synthetic values — a real capture names
    the networks around whoever recorded it, and a BSSID is geolocatable.
    Everything the tests actually assert on (line layout, ages, IE contents)
    is byte-for-byte as the device emitted it.
    """

    def setUp(self):
        self.obs = parse_iw_scan(REAL.read_text(), ts=1000.0)
        self.by_ssid = {o.ssid: o for o in self.obs}

    def test_parses_every_bss(self):
        self.assertEqual(len(self.obs), 5)

    def test_boottime_line_does_not_confuse_the_age_parser(self):
        # Each block carries BOTH `last seen: 1088.333s [boottime]` and
        # `last seen: 224 ms ago`; only the latter is an age.
        self.assertAlmostEqual(self.by_ssid["Wpa2Psk"].age_s, 0.224)

    def test_float_frequency_format(self):
        # iw 6.9 prints `freq: 2412.0`
        self.assertEqual(self.by_ssid["Wpa2Psk"].frequency, 2412)
        self.assertEqual(self.by_ssid["Wpa2Psk"].channel, 1)

    def test_stale_cache_entry_is_visible(self):
        # This is the kernel BSS cache in the wild: one of five entries was
        # 20 s old, which at road speed is a few hundred metres of error.
        self.assertGreater(self.by_ssid["StaleCacheEntry"].age_s, 20.0)

    def test_wpa3_transition_mode_in_the_wild(self):
        auth = self.by_ssid["Wpa3Transition"].auth
        self.assertIn("[WPA3-SAE-CCMP]", auth)
        self.assertIn("[WPA2-PSK-CCMP]", auth)

    def test_mixed_wpa_wpa2_in_the_wild(self):
        auth = self.by_ssid["MixedWpaWpa2"].auth
        self.assertIn("[WPA-PSK-TKIP]", auth)
        self.assertIn("[WPA2-PSK-CCMP+TKIP]", auth)

    def test_stale_filtering_drops_the_old_entry(self):
        sc = WifiScanner("wlan0")
        sc.last_scan_s = 2.0
        sc._ingest(REAL.read_text())
        kept = {o.ssid for o in sc.drain()}
        self.assertNotIn("StaleCacheEntry", kept)
        self.assertEqual(len(kept), 4)


if __name__ == "__main__":
    unittest.main()
