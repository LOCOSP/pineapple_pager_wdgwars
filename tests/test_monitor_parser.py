"""Tests for the monitor-mode capture decoder.

Frames are synthesised rather than captured so the fixtures stay readable and
the exact bytes under test are obvious.
"""

import os
import struct
import unittest

from . import conftest_path  # noqa: F401
from scanners.monitor import (
    CAP_ESS, CAP_IBSS, CAP_PRIVACY, DLT_IEEE802_11_RADIOTAP, HOP_2G,
    MonitorScanner, _channel_to_freq, _resolve_channel, decode_ssid,
    describe_bss, parse_ies, parse_mgmt_header, parse_radiotap, parse_rsn_ie,
)


# present bitmap: TSFT(0) | FLAGS(1) | RATE(2) | CHANNEL(3) | DBM_ANTSIGNAL(5)
_PRESENT = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 5)


def radiotap(freq=2437, signal=-52, flags=0):
    body = struct.pack("<Q", 123456)          # 8..16  TSFT
    body += struct.pack("<B", flags)          # 16     Flags
    body += struct.pack("<B", 2)              # 17     Rate
    body += struct.pack("<HH", freq, 0x00a0)  # 18..22 Channel
    body += struct.pack("<b", signal)         # 22     Signal
    hlen = 8 + len(body)
    return struct.pack("<BBHI", 0, 0, hlen, _PRESENT) + body


def ie(eid, payload):
    return bytes([eid, len(payload)]) + payload


def rsn_ie(group=4, pairwise=(4,), akms=(2,)):
    body = struct.pack("<H", 1)
    body += b"\x00\x0f\xac" + bytes([group])
    body += struct.pack("<H", len(pairwise))
    for c in pairwise:
        body += b"\x00\x0f\xac" + bytes([c])
    body += struct.pack("<H", len(akms))
    for a in akms:
        body += b"\x00\x0f\xac" + bytes([a])
    body += struct.pack("<H", 0)
    return body


def wpa_ie(group=2, pairwise=(2,), akms=(2,)):
    body = b"\x00\x50\xf2\x01" + struct.pack("<H", 1)
    body += b"\x00\x50\xf2" + bytes([group])
    body += struct.pack("<H", len(pairwise))
    for c in pairwise:
        body += b"\x00\x50\xf2" + bytes([c])
    body += struct.pack("<H", len(akms))
    for a in akms:
        body += b"\x00\x50\xf2" + bytes([a])
    return body


def beacon(bssid=b"\xaa\xbb\xcc\xdd\xee\xff", ssid=b"TestNet", channel=6,
           cap=CAP_ESS | CAP_PRIVACY, extra_ies=b"", subtype=8):
    fc0 = (subtype << 4) | (0 << 2)
    frame = bytes([fc0, 0x00]) + b"\x00\x00"
    frame += b"\xff" * 6 + bssid + bssid + b"\x00\x00"
    frame += b"\x00" * 8                       # timestamp
    frame += struct.pack("<H", 100)            # beacon interval
    frame += struct.pack("<H", cap)
    frame += ie(0, ssid) + ie(3, bytes([channel])) + extra_ies
    return frame


class TestRadiotap(unittest.TestCase):
    def test_extracts_freq_and_signal(self):
        hlen, freq, signal, flags = parse_radiotap(radiotap(5180, -71))
        self.assertEqual(freq, 5180)
        self.assertEqual(signal, -71)
        self.assertEqual(hlen, 23)

    def test_negative_signal_is_signed(self):
        _, _, signal, _ = parse_radiotap(radiotap(2412, -90))
        self.assertEqual(signal, -90)

    def test_rejects_bad_version(self):
        buf = bytearray(radiotap())
        buf[0] = 1
        self.assertIsNone(parse_radiotap(bytes(buf)))

    def test_rejects_truncated_header(self):
        self.assertIsNone(parse_radiotap(b"\x00\x00"))

    def test_absent_fields_read_as_zero(self):
        # present bitmap with only FLAGS set
        buf = struct.pack("<BBHI", 0, 0, 9, 1 << 1) + b"\x00"
        hlen, freq, signal, flags = parse_radiotap(buf)
        self.assertEqual((freq, signal), (0, 0))


class TestMgmtHeader(unittest.TestCase):
    def test_beacon_is_accepted(self):
        head = parse_mgmt_header(beacon())
        self.assertIsNotNone(head)
        subtype, bssid, cap, ies = head
        self.assertEqual(subtype, 8)
        self.assertEqual(bssid, b"\xaa\xbb\xcc\xdd\xee\xff")
        self.assertTrue(cap & CAP_PRIVACY)

    def test_probe_response_is_accepted(self):
        self.assertIsNotNone(parse_mgmt_header(beacon(subtype=5)))

    def test_other_subtypes_rejected(self):
        self.assertIsNone(parse_mgmt_header(beacon(subtype=4)))  # probe req

    def test_data_frames_rejected(self):
        frame = bytearray(beacon())
        frame[0] = 0x08                       # type=2 (data)
        self.assertIsNone(parse_mgmt_header(bytes(frame)))

    def test_short_frame_rejected(self):
        self.assertIsNone(parse_mgmt_header(b"\x80\x00" + b"\x00" * 10))


class TestIes(unittest.TestCase):
    def test_ssid_and_channel(self):
        _, _, _, ies = parse_mgmt_header(beacon(ssid=b"MyNet", channel=11))
        parsed = parse_ies(ies)
        self.assertEqual(parsed["ssid"], b"MyNet")
        self.assertEqual(parsed["channel"], 11)

    def test_truncated_ie_does_not_explode(self):
        parsed = parse_ies(b"\x00\x40ab")      # claims 64 bytes, has 2
        self.assertEqual(parsed["ssid"], b"")

    def test_wps_vendor_ie(self):
        wps = b"\x00\x50\xf2\x04\x10\x4a"
        _, _, _, ies = parse_mgmt_header(beacon(extra_ies=ie(221, wps)))
        self.assertTrue(parse_ies(ies)["wps"])


class TestRsnDecoding(unittest.TestCase):
    def test_wpa2_psk(self):
        got = parse_rsn_ie(rsn_ie(akms=(2,)))
        self.assertEqual(got["ciphers"], ["CCMP"])
        self.assertEqual(got["akms"], {"PSK"})

    def test_enterprise(self):
        self.assertEqual(parse_rsn_ie(rsn_ie(akms=(1,)))["akms"], {"EAP"})

    def test_sae(self):
        self.assertEqual(parse_rsn_ie(rsn_ie(akms=(8,)))["akms"], {"SAE"})

    def test_owe(self):
        self.assertEqual(parse_rsn_ie(rsn_ie(akms=(18,)))["akms"], {"OWE"})

    def test_transition_mode(self):
        self.assertEqual(parse_rsn_ie(rsn_ie(akms=(2, 8)))["akms"],
                         {"PSK", "SAE"})

    def test_suite_b(self):
        self.assertIn("EAP-SUITE-B", parse_rsn_ie(rsn_ie(akms=(12,)))["akms"])

    def test_mixed_ciphers(self):
        got = parse_rsn_ie(rsn_ie(group=2, pairwise=(4, 2)))
        self.assertIn("CCMP", got["ciphers"])
        self.assertIn("TKIP", got["ciphers"])

    def test_foreign_oui_is_ignored(self):
        body = struct.pack("<H", 1) + b"\xde\xad\xbe\x04" + struct.pack("<H", 0)
        self.assertEqual(parse_rsn_ie(body)["ciphers"], [])

    def test_absurd_counts_are_clamped(self):
        body = struct.pack("<H", 1) + b"\x00\x0f\xac\x04" + struct.pack("<H", 60000)
        got = parse_rsn_ie(body)          # must not hang or raise
        self.assertEqual(got["akms"], set())


class TestDescribeBss(unittest.TestCase):
    def test_matches_the_iw_backend_spelling(self):
        _, _, cap, ies = parse_mgmt_header(beacon(extra_ies=ie(48, rsn_ie())))
        ssid, auth, ch = describe_bss(cap, parse_ies(ies))
        self.assertEqual(ssid, "TestNet")
        self.assertEqual(auth, "[WPA2-PSK-CCMP][ESS]")
        self.assertEqual(ch, 6)

    def test_open_network(self):
        _, _, cap, ies = parse_mgmt_header(beacon(cap=CAP_ESS))
        _, auth, _ = describe_bss(cap, parse_ies(ies))
        self.assertEqual(auth, "[ESS]")

    def test_wep(self):
        _, _, cap, ies = parse_mgmt_header(beacon(cap=CAP_ESS | CAP_PRIVACY))
        _, auth, _ = describe_bss(cap, parse_ies(ies))
        self.assertEqual(auth, "[WEP][ESS]")

    def test_ibss(self):
        _, _, cap, ies = parse_mgmt_header(beacon(cap=CAP_IBSS))
        _, auth, _ = describe_bss(cap, parse_ies(ies))
        self.assertEqual(auth, "[IBSS]")

    def test_wpa_and_rsn(self):
        extra = ie(221, wpa_ie()) + ie(48, rsn_ie())
        _, _, cap, ies = parse_mgmt_header(beacon(extra_ies=extra))
        _, auth, _ = describe_bss(cap, parse_ies(ies))
        self.assertEqual(auth, "[WPA-PSK-TKIP][WPA2-PSK-CCMP][ESS]")

    def test_hidden_ssid(self):
        _, _, cap, ies = parse_mgmt_header(beacon(ssid=b"\x00\x00\x00"))
        ssid, _, _ = describe_bss(cap, parse_ies(ies))
        self.assertEqual(ssid, "")

    def test_utf8_ssid_survives(self):
        _, _, cap, ies = parse_mgmt_header(
            beacon(ssid="Dom Kowalskichł".encode("utf-8")))
        ssid, _, _ = describe_bss(cap, parse_ies(ies))
        self.assertEqual(ssid, "Dom Kowalskichł")


def pcap(records, linktype=DLT_IEEE802_11_RADIOTAP):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype)
    for ts_sec, ts_usec, data in records:
        out += struct.pack("<IIII", ts_sec, ts_usec, len(data), len(data))
        out += data
    return out


class FakeProc:
    def __init__(self, fd):
        self.stdout = os.fdopen(fd, "rb", buffering=0)

    def poll(self):
        return None


class TestCaptureLoop(unittest.TestCase):
    """Feed a synthetic pcap through the real read loop over a pipe."""

    def _run(self, records, **kwargs):
        r, w = os.pipe()
        sc = MonitorScanner("wlan9mon", hop=False, emit_interval_s=0.0, **kwargs)
        sc._proc = FakeProc(r)
        self.addCleanup(sc._proc.stdout.close)
        os.write(w, pcap(records))
        os.close(w)
        sc._read_loop()
        return sc

    def test_decodes_a_beacon_into_an_observation(self):
        frame = radiotap(2437, -52) + beacon(extra_ies=ie(48, rsn_ie()))
        sc = self._run([(1700000000, 500000, frame)])
        obs = sc.drain()
        self.assertEqual(len(obs), 1)
        o = obs[0]
        self.assertEqual(o.bssid, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(o.ssid, "TestNet")
        self.assertEqual(o.rssi, -52)
        self.assertEqual(o.frequency, 2437)
        self.assertEqual(o.channel, 6)
        self.assertEqual(o.auth, "[WPA2-PSK-CCMP][ESS]")

    def test_timestamp_comes_from_the_capture_not_write_time(self):
        frame = radiotap() + beacon()
        sc = self._run([(1700000000, 250000, frame)])
        self.assertAlmostEqual(sc.drain()[0].first_seen, 1700000000.25,
                               places=3)

    def test_repeated_beacons_aggregate_to_the_strongest(self):
        frames = [(1700000000, i * 1000,
                   radiotap(2437, rssi) + beacon())
                  for i, rssi in enumerate((-70, -55, -80))]
        sc = self._run(frames)
        obs = sc.drain()
        self.assertEqual(len(obs), 1)          # one AP, one row's worth
        self.assertEqual(obs[0].rssi, -55)     # closest approach wins

    def test_ies_are_parsed_once_per_bssid(self):
        frame = radiotap() + beacon(extra_ies=ie(48, rsn_ie()))
        sc = self._run([(1700000000, i, frame) for i in range(25)])
        self.assertEqual(len(sc._info), 1)
        self.assertEqual(sc.frames, 25)

    def test_distinct_bssids_are_kept_apart(self):
        frames = [
            (1700000000, 0, radiotap(2437, -50) +
             beacon(bssid=b"\x11" * 6, ssid=b"A")),
            (1700000000, 1000, radiotap(5180, -75) +
             beacon(bssid=b"\x22" * 6, ssid=b"B", channel=36)),
        ]
        sc = self._run(frames)
        obs = {o.ssid: o for o in sc.drain()}
        self.assertEqual(set(obs), {"A", "B"})
        self.assertEqual(obs["B"].frequency, 5180)

    def test_bad_fcs_frames_are_dropped(self):
        frame = radiotap(2437, -52, flags=0x40) + beacon()
        sc = self._run([(1700000000, 0, frame)])
        self.assertEqual(sc.drain(), [])
        self.assertEqual(sc.bad_fcs, 1)

    def test_probe_requests_and_data_frames_are_ignored(self):
        data_frame = bytearray(beacon())
        data_frame[0] = 0x08
        frames = [
            (1700000000, 0, radiotap() + beacon(subtype=4)),
            (1700000000, 1, radiotap() + bytes(data_frame)),
        ]
        sc = self._run(frames)
        self.assertEqual(sc.drain(), [])

    def test_split_records_across_reads_are_reassembled(self):
        """The pipe hands us arbitrary chunk boundaries, not whole packets."""
        frame = radiotap() + beacon()
        blob = pcap([(1700000000, 0, frame), (1700000000, 1, frame)])
        r, w = os.pipe()
        sc = MonitorScanner("wlan9mon", hop=False, emit_interval_s=0.0)
        sc._proc = FakeProc(r)
        self.addCleanup(sc._proc.stdout.close)
        for i in range(0, len(blob), 7):        # deliberately awkward chunks
            os.write(w, blob[i:i + 7])
        os.close(w)
        sc._read_loop()
        self.assertEqual(len(sc.drain()), 1)
        self.assertEqual(sc.frames, 2)

    def test_hidden_beacon_does_not_erase_a_known_ssid(self):
        named = radiotap() + beacon(ssid=b"RealName")
        hidden = radiotap() + beacon(ssid=b"\x00" * 8)
        sc = self._run([(1700000000, 0, named), (1700000200, 0, hidden)],
                       ie_refresh_s=1.0)
        self.assertEqual(sc._info[b"\xaa\xbb\xcc\xdd\xee\xff"][0], "RealName")

    def test_unexpected_magic_is_reported(self):
        r, w = os.pipe()
        sc = MonitorScanner("wlan9mon", hop=False)
        sc._proc = FakeProc(r)
        self.addCleanup(sc._proc.stdout.close)
        os.write(w, b"\x00" * 24)
        os.close(w)
        sc._read_loop()
        self.assertIn("magic", sc.last_error or "")


class TestHopPlan(unittest.TestCase):
    def test_plan_is_limited_to_supported_frequencies(self):
        sc = MonitorScanner("wlan9mon")
        sc._supported_freqs = lambda: {2412, 2437, 2462}
        plan = sc._build_hop_plan()
        self.assertTrue(set(plan) <= {2412, 2437, 2462})

    def test_busy_channels_are_revisited(self):
        sc = MonitorScanner("wlan9mon")
        sc._supported_freqs = lambda: set(HOP_2G) | {5180, 5200, 5220}
        plan = sc._build_hop_plan()
        self.assertGreater(plan.count(2437) + plan.count(2412) +
                           plan.count(2462), 3)

    def test_no_supported_frequencies_still_yields_a_plan(self):
        sc = MonitorScanner("wlan9mon")
        sc._supported_freqs = lambda: set()
        self.assertTrue(sc._build_hop_plan())


class TestHelpers(unittest.TestCase):
    def test_decode_ssid_empty(self):
        self.assertEqual(decode_ssid(b""), "")

    def test_channel_to_freq(self):
        self.assertEqual(_channel_to_freq(6), 2437)
        self.assertEqual(_channel_to_freq(14), 2484)
        self.assertEqual(_channel_to_freq(36), 5180)
        self.assertEqual(_channel_to_freq(0), 0)


class TestTcpdumpStderrFiltering(unittest.TestCase):
    """tcpdump talks on stderr when nothing is wrong. Captured verbatim from
    the Pager: the exit summary was surfacing as a HUD warning."""

    def _noise(self, line):
        return bool(MonitorScanner._TCPDUMP_NOISE.match(line))

    def test_startup_banner_is_not_an_error(self):
        self.assertTrue(self._noise(
            "tcpdump: listening on wlan1mon, link-type IEEE802_11_RADIO"))

    def test_exit_summary_is_not_an_error(self):
        for line in ("0 packets dropped by kernel",
                     "124 packets captured",
                     "124 packets received by filter",
                     "0 packets dropped by interface"):
            self.assertTrue(self._noise(line), line)

    def test_real_errors_still_surface(self):
        for line in ("tcpdump: wlan9mon: No such device exists",
                     "tcpdump: syntax error in filter expression",
                     "tcpdump: (cannot open device) permission denied"):
            self.assertFalse(self._noise(line), line)


class TestChannelResolution(unittest.TestCase):
    """Channel and Frequency must describe the *AP*, not the receiver.

    Observed on the Pager: a beacon from channel 8 heard while the hopper sat
    on 2442 MHz produced the row "channel 8, 2442 MHz" — 2442 is channel 7.
    """

    def test_overlapping_24ghz_prefers_the_ap_declared_channel(self):
        freq, ch = _resolve_channel(2442, 8)     # heard on ch7, AP says ch8
        self.assertEqual((freq, ch), (2447, 8))

    def test_matching_channel_is_unchanged(self):
        self.assertEqual(_resolve_channel(2437, 6), (2437, 6))

    def test_5ghz_ie_channel_is_honoured(self):
        self.assertEqual(_resolve_channel(5540, 108), (5540, 108))

    def test_cross_band_ie_channel_is_rejected(self):
        # A bare channel number is ambiguous; never let it move an AP between
        # bands. 6 GHz frames carry no DS/HT element, so this is the guard.
        freq, ch = _resolve_channel(5955, 37)
        self.assertEqual(freq, 5955)
        self.assertEqual(ch, 1)          # 5955 MHz is 6 GHz channel 1

    def test_no_ie_channel_falls_back_to_receive_frequency(self):
        self.assertEqual(_resolve_channel(5200, 0), (5200, 40))

    def test_no_receive_frequency_uses_the_ie(self):
        self.assertEqual(_resolve_channel(0, 11), (2462, 11))

    def test_row_channel_and_frequency_always_agree(self):
        from scanners.wifi import _freq_to_channel
        for rx, ie in ((2442, 8), (2437, 6), (5540, 108), (5200, 0), (0, 11),
                       (2412, 13), (5955, 37)):
            freq, ch = _resolve_channel(rx, ie)
            if freq:
                self.assertEqual(_freq_to_channel(freq), ch,
                                 f"rx={rx} ie={ie} -> {freq}/{ch}")


class TestHtOperationChannel(unittest.TestCase):
    def test_ht_operation_supplies_a_channel_when_ds_param_is_absent(self):
        ht = ie(61, bytes([36, 0, 0, 0, 0]))
        parsed = parse_ies(ie(0, b"Net") + ht)
        self.assertEqual(parsed["channel"], 36)

    def test_ds_param_wins_over_ht_operation(self):
        both = ie(0, b"Net") + ie(3, bytes([6])) + ie(61, bytes([36, 0]))
        self.assertEqual(parse_ies(both)["channel"], 6)


if __name__ == "__main__":
    unittest.main()
