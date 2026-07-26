"""Import-level smoke test for the app entry point.

`wdgwars.py` needs `pagerctl`, which only exists on the device (bootstrap
copies it out of the wifman payload). Stubbing it lets the test suite catch
NameErrors, bad imports and typos in the menu wiring, which would otherwise
only show up on the pager itself.
"""

import sys
import types
import unittest
from pathlib import Path

from . import conftest_path  # noqa: F401


def _install_pagerctl_stub():
    if "pagerctl" in sys.modules:
        return
    mod = types.ModuleType("pagerctl")

    class Pager:
        width, height = 480, 222
        BTN_A = BTN_B = BTN_UP = BTN_DOWN = 0
        EVENT_PRESS = 1

        def init(self):
            return 0

    mod.Pager = Pager
    sys.modules["pagerctl"] = mod


_install_pagerctl_stub()
import wdgwars as app  # noqa: E402


class TestModuleLoads(unittest.TestCase):
    def test_entry_points_exist(self):
        for name in ("main", "App", "load_config", "save_config"):
            self.assertTrue(hasattr(app, name), name)

    def test_app_exposes_the_wired_up_actions(self):
        for name in ("_live_scan", "_start_wifi", "_action_sync",
                     "_action_history", "_action_sessions", "_cfg_scan",
                     "_cfg_wifi_source", "_show_history_entry"):
            self.assertTrue(callable(getattr(app.App, name, None)), name)

    def test_band_presets_reference_real_bands(self):
        from scanners.wifi import BANDS
        for _label, plan in app.App.BAND_PRESETS:
            for band in plan:
                self.assertIn(band, BANDS)

    def test_shipped_config_parses_and_covers_the_new_keys(self):
        import json
        cfg = json.loads((Path(app.__file__).parent / "config.json").read_text())
        scan = cfg["scan"]
        for key in ("wifi_iface", "band_plan", "min_move_m", "refresh_ttl_s",
                    "require_fix", "monitor_hop"):
            self.assertIn(key, scan)
        self.assertIn("mode", cfg["upload"])


class TestRateHelper(unittest.TestCase):
    def test_no_samples(self):
        self.assertEqual(app._rows_per_min([]), 0.0)

    def test_too_short_a_window(self):
        self.assertEqual(app._rows_per_min([(0.0, 0), (1.0, 10)]), 0.0)

    def test_steady_rate(self):
        hist = [(float(t), t * 2) for t in range(0, 31, 5)]
        self.assertAlmostEqual(app._rows_per_min(hist), 120.0, delta=1.0)

    def test_only_the_last_minute_counts(self):
        hist = [(0.0, 0), (10.0, 1000)] + [(60.0 + t, 1000) for t in range(0, 31, 5)]
        self.assertAlmostEqual(app._rows_per_min(hist), 0.0, delta=1.0)


class TestRowCounter(unittest.TestCase):
    def test_counts_data_rows_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "wd.csv"
            p.write_text("header\ncolumns\na\nb\nc\n")
            self.assertEqual(app._count_rows(p), 3)

    def test_empty_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "wd.csv"
            p.write_text("")
            self.assertEqual(app._count_rows(p), 0)

    def test_missing_file(self):
        self.assertEqual(app._count_rows(Path("/nonexistent/wd.csv")), 0)


class TestMaskKey(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(app._mask_key(""), "(empty)")

    def test_masks_the_middle(self):
        masked = app._mask_key("a" * 64)
        self.assertIn("...", masked)
        self.assertLess(len(masked), 64)


if __name__ == "__main__":
    unittest.main()
