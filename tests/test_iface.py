import unittest

from . import conftest_path  # noqa: F401
from scanners.iface import IfaceInfo, parse_iw_dev, pick_wifi_source


IW_DEV = """phy#2
	Interface wlan2mon
		ifindex 14
		wdev 0x200000001
		addr 00:c0:ca:11:22:33
		type monitor
		txpower 20.00 dBm
phy#1
	Interface wlan1
		ifindex 11
		addr 00:13:37:00:00:02
		type managed
phy#0
	Interface wlan0mgmt
		ifindex 9
		addr 00:13:37:00:00:01
		type AP
	Interface wlan0
		ifindex 8
		addr 00:13:37:00:00:00
		type managed
"""


def up(name, itype, phy):
    return IfaceInfo(name, itype, phy, True)


class TestParseIwDev(unittest.TestCase):
    def setUp(self):
        self.ifaces = parse_iw_dev(IW_DEV)

    def test_finds_all_interfaces(self):
        self.assertEqual({i.name for i in self.ifaces},
                         {"wlan2mon", "wlan1", "wlan0mgmt", "wlan0"})

    def test_types_and_phys(self):
        by_name = {i.name: i for i in self.ifaces}
        self.assertEqual(by_name["wlan2mon"].type, "monitor")
        self.assertEqual(by_name["wlan2mon"].phy, 2)
        self.assertTrue(by_name["wlan2mon"].is_monitor)
        self.assertEqual(by_name["wlan0"].phy, 0)
        self.assertEqual(by_name["wlan0mgmt"].type, "AP")

    def test_pager_owned_flag(self):
        by_name = {i.name: i for i in self.ifaces}
        self.assertTrue(by_name["wlan0mgmt"].is_pager_owned)
        self.assertFalse(by_name["wlan1"].is_pager_owned)

    def test_empty_input(self):
        self.assertEqual(parse_iw_dev(""), [])


class TestPickWifiSource(unittest.TestCase):
    """Issue #3: an external AWUS036ACM staged as wlan2mon should be used."""

    def test_auto_prefers_a_live_monitor_interface(self):
        ifaces = [up("wlan2mon", "monitor", 2), up("wlan0", "managed", 0)]
        mode, iface, _ = pick_wifi_source("auto", ifaces)
        self.assertEqual((mode, iface), ("monitor", "wlan2mon"))

    def test_down_monitor_interface_is_ignored(self):
        ifaces = [IfaceInfo("wlan2mon", "monitor", 2, False),
                  up("wlan0", "managed", 0)]
        mode, iface, _ = pick_wifi_source("auto", ifaces)
        self.assertEqual((mode, iface), ("scan", "wlan0"))

    def test_auto_prefers_external_managed_over_pager_management_radio(self):
        ifaces = [up("wlan0mgmt", "AP", 0), up("wlan0", "managed", 0),
                  up("wlan1", "managed", 1)]
        mode, iface, _ = pick_wifi_source("auto", ifaces)
        self.assertEqual((mode, iface), ("scan", "wlan1"))

    def test_explicit_name_is_honoured(self):
        ifaces = [up("wlan2mon", "monitor", 2), up("wlan0", "managed", 0)]
        mode, iface, _ = pick_wifi_source("wlan0", ifaces)
        self.assertEqual((mode, iface), ("scan", "wlan0"))

    def test_explicit_unknown_name_is_still_used(self):
        mode, iface, why = pick_wifi_source("wlan9mon", [])
        self.assertEqual((mode, iface), ("monitor", "wlan9mon"))
        self.assertIn("not in", why)

    def test_force_monitor_without_one_degrades_to_scan(self):
        ifaces = [up("wlan0", "managed", 0)]
        mode, iface, why = pick_wifi_source("monitor", ifaces)
        self.assertEqual((mode, iface), ("scan", "wlan0"))
        self.assertIn("none up", why)

    def test_force_scan_ignores_monitor(self):
        ifaces = [up("wlan2mon", "monitor", 2), up("wlan0", "managed", 0)]
        mode, iface, _ = pick_wifi_source("scan", ifaces)
        self.assertEqual((mode, iface), ("scan", "wlan0"))

    def test_no_interfaces_falls_back_to_wlan0(self):
        mode, iface, _ = pick_wifi_source("auto", [])
        self.assertEqual((mode, iface), ("scan", "wlan0"))


if __name__ == "__main__":
    unittest.main()
