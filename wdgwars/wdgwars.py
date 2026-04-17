"""Entry point for the WDGoWars Wardriver payload.

Boot sequence: load config -> init Pager -> splash -> main menu loop.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Allow `python3 wdgwars.py` to find the bundled `lib/pagerctl.py` even when
# PYTHONPATH was not set by payload.sh (useful for local debugging).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "lib"))
sys.path.insert(0, str(_HERE))

from pagerctl import Pager  # noqa: E402

from ui import theme, splash, menu, dialog, status as hud, keyboard, idle  # noqa: E402
from scanners.wifi import WifiScanner  # noqa: E402
from scanners.ble import BleScanner  # noqa: E402
from scanners.gps import GpsReader  # noqa: E402
from storage.session import (  # noqa: E402
    Session, list_pending, list_all, mark_uploaded, mark_error,
)
from uploader import wdgwars as api  # noqa: E402
import handoff  # noqa: E402


CONFIG_PATH = _HERE / "config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def main() -> int:
    cfg = load_config()

    p = Pager()
    if p.init() != 0:
        print("pager init failed", file=sys.stderr)
        return 1
    result = None
    try:
        with p:
            ui_cfg = cfg.get("ui", {})
            try:
                p.set_rotation(int(ui_cfg.get("rotation", 270)))
                p.set_brightness(int(ui_cfg.get("brightness", 70)))
            except Exception:
                pass
            idle.init(p,
                      timeout_s=ui_cfg.get("idle_timeout_s", 20),
                      brightness=ui_cfg.get("brightness", 70),
                      dim_level=ui_cfg.get("auto_dim_level", 10))
            pal = theme.Palette(p)
            splash.show(p, pal)
            result = App(p, pal, cfg).run()
    finally:
        p.cleanup()
    # Return 42 for handoff, 0 for normal exit — payload.sh uses the exit
    # code to decide whether to re-exec into the next payload.
    return handoff.HANDOFF_EXIT_CODE if result == handoff.HANDOFF_SENTINEL else 0


class App:
    def __init__(self, p, pal, cfg: dict) -> None:
        self.p = p
        self.pal = pal
        self.cfg = cfg
        self.gps = GpsReader(
            cfg.get("gps", {}).get("devices", ["/dev/ttyACM0"]),
            baud=cfg.get("gps", {}).get("baud", 9600),
            min_sats=cfg.get("gps", {}).get("min_sats", 4),
        )
        self.gps.start()
        self.loot_dir = Path(cfg.get("storage", {}).get("loot_dir", "/mmc/root/loot/wdgwars"))
        self.loot_dir.mkdir(parents=True, exist_ok=True)
        (self.loot_dir / "sessions").mkdir(exist_ok=True)

    @property
    def sessions_dir(self) -> Path:
        return self.loot_dir / "sessions"

    def run(self) -> str | None:
        """Returns None on normal exit, handoff.HANDOFF_SENTINEL if the user
        picked a JUMP TO target — main() translates that to `return 42`."""
        try:
            while True:
                r = self._main_menu()
                if r == "exit":
                    return None
                if r == handoff.HANDOFF_SENTINEL:
                    return r
        finally:
            try:
                self.gps.stop()
            except Exception:
                pass

    def _main_menu(self):
        def build():
            pending = len(list_pending(self.sessions_dir))
            all_count = len(list_all(self.sessions_dir))
            peers = handoff.discover(_HERE)
            items = [
                menu.MenuItem("WARDRIVE BOTH",
                              action=lambda: self._action_scan(wifi=True, ble=True)),
                menu.MenuItem("WARDRIVE WIFI",
                              action=lambda: self._action_scan(wifi=True, ble=False)),
                menu.MenuItem("WARDRIVE BT",
                              action=lambda: self._action_scan(wifi=False, ble=True)),
                menu.MenuItem("SYNC NOW", action=lambda: self._action_sync(),
                              badge=f"Q:{pending}" if pending else None),
                menu.MenuItem("SESSIONS", action=lambda: self._action_sessions(),
                              badge=str(all_count) if all_count else None),
                menu.MenuItem("CONFIG", action=lambda: self._action_config()),
            ]
            if peers:
                items.append(menu.MenuItem("JUMP TO ...",
                                           action=lambda: self._action_jump(peers),
                                           badge=str(len(peers))))
            items.append(menu.MenuItem("POWER OFF", action=lambda: self._action_exit()))
            return items
        return menu.run(self.p, self.pal, "MAIN", build, on_back=lambda: None)

    def _action_jump(self, peers):
        items = [
            menu.MenuItem(p.title,
                          action=lambda lp=p.path: self._do_handoff(lp))
            for p in peers
        ]
        items.append(menu.MenuItem("(cancel)", action=lambda: "back"))
        # menu.run returns whatever the picked action returned. If it was a
        # handoff, that's handoff.HANDOFF_SENTINEL — bubble it up so run()
        # and main() can translate it to `return 42`.
        return menu.run(self.p, self.pal, "JUMP TO", items)

    def _do_handoff(self, launcher_path: str) -> str:
        # Tear down GPS thread cleanly so the next payload owns the serial port.
        try:
            self.gps.stop()
        except Exception:
            pass
        # Kill any stray bluetoothctl we might still have running — it holds
        # hci0 exclusive and would starve the peer payload if left alive.
        try:
            import subprocess as _sp
            _sp.run(["killall", "-q", "bluetoothctl"], check=False, timeout=2)
        except Exception:
            pass
        # Hint screen, write .next_payload, return the HANDOFF sentinel. The
        # sentinel bubbles up through menu.run → _main_menu → App.run → main(),
        # which translates it into `return 42`. No mid-call sys.exit so every
        # `finally:` block fires in order — pagergotchi's pattern.
        dialog.alert(self.p, self.pal, "HANDOFF",
                     f"Switching to:\n{Path(launcher_path).name}",
                     accent=self.pal.cyan)
        return handoff.request_handoff(_HERE, launcher_path)

    # ---------------- actions ---------------- #

    def _action_scan(self, wifi: bool = True, ble: bool = True):
        if not self._wait_for_gps():
            return
        self._live_scan(use_wifi=wifi, use_ble=ble)

    def _wait_for_gps(self) -> bool:
        if self.gps.state.snapshot().fix_3d:
            return True

        def live_msg() -> str:
            s = self.gps.state.snapshot()
            dev = s.device or "(no device)"
            fq_label = {0: "no fix", 1: "GPS", 2: "DGPS", 4: "RTK fix", 5: "RTK float"}.get(
                s.fix_quality, f"fq{s.fix_quality}")
            return (f"Waiting for u-blox 7 fix.\n"
                    f"dev: {dev}\n"
                    f"sats: {s.sats}   {fq_label}\n"
                    f"need >= {self.gps.min_sats} sats + 3D fix\n"
                    f"\nB to cancel")

        ok = dialog.wait_with(
            self.p, self.pal,
            title="GPS",
            message="",
            poll=lambda: self.gps.state.snapshot().fix_3d,
            timeout_ms=0,
            live_message=live_msg,
        )
        if not ok:
            dialog.alert(self.p, self.pal, "GPS",
                         "Scan aborted.\nNo fix yet.", accent=self.pal.amber)
        return ok

    def _live_scan(self, use_wifi: bool = True, use_ble: bool = True) -> None:
        scan_cfg = self.cfg.get("scan", {})
        wifi = WifiScanner("wlan0", interval_s=scan_cfg.get("wifi_interval_s", 8)) if use_wifi else None
        ble  = BleScanner("hci0",   interval_s=scan_cfg.get("ble_interval_s", 12))  if use_ble  else None
        if wifi:
            wifi.start()
        if ble:
            ble.start()

        if wifi and wifi.last_error:
            dialog.alert(self.p, self.pal, "WIFI",
                         f"WiFi disabled:\n{wifi.last_error}", accent=self.pal.amber)
        if ble and not ble.available:
            dialog.alert(self.p, self.pal, "BLE",
                         f"BLE disabled:\n{ble.last_error or 'hci0 missing'}",
                         accent=self.pal.amber)

        sess = Session(
            self.sessions_dir,
            max_file_mb=self.cfg.get("storage", {}).get("max_file_mb", 30),
            dedup_ttl_s=scan_cfg.get("dedup_ttl_s", 60),
        )
        st = hud.HudState(session_id=sess.session_id)

        def adjust_brightness(delta: int) -> None:
            mgr = idle.get()
            new = max(5, min(100, (mgr.brightness if mgr else 70) + delta))
            if mgr:
                mgr.set_brightness(new)
            else:
                try:
                    self.p.set_brightness(new)
                except Exception:
                    pass
            self.cfg.setdefault("ui", {})["brightness"] = new
            save_config(self.cfg)

        # Run loop manually (we need to drain scanners between renders).
        try:
            while True:
                gps_snap = self.gps.state.snapshot()
                st.gps_fix = gps_snap.fix_3d
                st.gps_sats = gps_snap.sats
                st.lat = gps_snap.lat
                st.lon = gps_snap.lon

                if not st.paused:
                    if wifi:
                        new_w = 0
                        for obs in wifi.drain():
                            st.wifi_total += 1
                            st.rssi_window.append(obs.rssi)
                            st.rssi_window = st.rssi_window[-64:]
                            if sess.add_wifi(obs, gps_snap):
                                new_w += 1
                        st.wifi_new = new_w if new_w else st.wifi_new

                    if ble:
                        new_b = 0
                        for obs in ble.drain():
                            st.ble_total += 1
                            if sess.add_ble(obs, gps_snap):
                                new_b += 1
                        st.ble_new = new_b if new_b else st.ble_new

                    st.queue_rows = sess.stats.rows_written

                # Render & flip only when backlight is on; saves CPU when asleep.
                mgr = idle.get()
                asleep = mgr.tick() if mgr else False
                if not asleep:
                    hud.render(self.p, self.pal, st)
                    self.p.flip()

                if self.p.has_input_events():
                    ev = self.p.get_input_event()
                    if ev:
                        btn, etype, _ = ev
                        if etype == getattr(self.p, "EVENT_PRESS", 1):
                            # First press while asleep just wakes the screen
                            if mgr and mgr.wake_consume():
                                while self.p.has_input_events():
                                    self.p.get_input_event()
                            elif btn == self.p.BTN_A:
                                st.paused = not st.paused
                            elif btn == self.p.BTN_B:
                                if dialog.confirm(self.p, self.pal, "END SESSION",
                                                  f"Stop scan and save\n{sess.stats.rows_written} rows?"):
                                    break
                            elif btn == self.p.BTN_UP:
                                adjust_brightness(+10)
                            elif btn == self.p.BTN_DOWN:
                                adjust_brightness(-10)

                self.p.delay(200)
        finally:
            if wifi:
                wifi.stop()
            if ble:
                ble.stop()
            sess.close()

        dialog.alert(self.p, self.pal, "SAVED",
                     f"Wrote {sess.stats.rows_written} rows\n"
                     f"WiFi: {sess.stats.wifi_total}  BLE: {sess.stats.ble_total}\n"
                     f"File: {Path(sess.stats.files[-1]).name}",
                     accent=self.pal.green)

    def _action_sync(self):
        api_key = self.cfg.get("api_key", "").strip()
        if not api_key:
            dialog.alert(self.p, self.pal, "SYNC",
                         "No API key configured.\nGo to CONFIG.", accent=self.pal.red)
            return

        pending = list_pending(self.sessions_dir)
        if not pending:
            dialog.alert(self.p, self.pal, "SYNC",
                         "Queue is empty.\nNothing to upload.", accent=self.pal.cyan)
            return

        # Connectivity + key check up front. We use /api/me as a combined
        # reachability + auth probe — single round-trip tells us whether the
        # pager has internet AND whether the key is still valid, so we can
        # bail with a meaningful message before touching any CSV.
        probe = api.me(api_key, timeout=8.0)
        if not probe.ok:
            if probe.status == 0:
                # urllib returned before hitting the server — no route / no DNS.
                dialog.alert(
                    self.p, self.pal, "SYNC",
                    "No internet connection.\n\n"
                    "Connect to WiFi first\n"
                    "(use JUMP TO -> WiFMan\n"
                    "or the pager menu).\n\n"
                    "Your sessions stay queued.",
                    accent=self.pal.amber)
            elif probe.status == 401:
                dialog.alert(
                    self.p, self.pal, "SYNC",
                    "API key rejected (401).\nFix it in CONFIG.",
                    accent=self.pal.red)
            else:
                dialog.alert(
                    self.p, self.pal, "SYNC",
                    f"Server unreachable.\nhttp {probe.status}\n"
                    f"{(probe.error or '')[:40]}",
                    accent=self.pal.red)
            return
        before = probe
        before_badges = set(before.badges or [])

        prog = dialog.Progress(self.p, self.pal, "SYNC")
        total = len(pending)
        done = 0
        merged_total = 0
        aborted = False

        for i, csv in enumerate(pending):
            prog.set(i / total, f"-> {csv.name} ({csv.stat().st_size // 1024}k)", self.pal.fg)
            res = api.upload_with_retry(api_key, csv,
                                        on_attempt=lambda a, msg: prog.set(i / total, msg, self.pal.fg_dim))
            if res.ok:
                mark_uploaded(csv, res.body)
                merged_total += res.merged_samples
                prog.set((i + 1) / total,
                         f"[OK] {csv.name}  +{res.merged_samples}", self.pal.green)
                done += 1
            else:
                msg = res.error or f"http {res.status}"
                mark_error(csv, msg)
                prog.set((i + 1) / total, f"[FAIL {res.status}] {msg[:32]}", self.pal.red)
                if res.status == 401:
                    aborted = True
                    break
            time.sleep(api.RATE_LIMIT_SLEEP_S)

        prog.set(1.0, f"== done  {done}/{total}  merged:{merged_total}",
                 self.pal.cyan if done else self.pal.amber)
        prog.wait_dismiss()
        if aborted:
            dialog.alert(self.p, self.pal, "SYNC",
                         "API key rejected (401).\nFix it in CONFIG.",
                         accent=self.pal.red)
            return

        # Diff badges after the upload — if /api/me added any, flash them.
        if done and before.ok:
            after = api.me(api_key, timeout=8.0)
            if after.ok:
                new_badges = [b for b in (after.badges or []) if b not in before_badges]
                if new_badges:
                    self._show_new_badges(new_badges, after)

    def _show_new_badges(self, new_badges: list[str], me_resp) -> None:
        # Pretty-print known badge IDs; fall back to the raw slug.
        pretty = {
            "hak5_pager_user":  "Hak5 Pager Op",
            "wardriver":        "Wardriver",
            "wifi_100":         "WiFi 100",
            "wifi_1k":          "WiFi 1k",
            "wifi_10k":         "WiFi 10k",
            "ble_100":          "BLE 100",
            "ble_1k":           "BLE 1k",
            "first_blood":      "First Blood",
            "globe_trotter":    "Globe Trotter",
            "wigle_user":       "Wigle User",
        }
        names = [pretty.get(b, b) for b in new_badges]
        body = "\n".join(f"+ {n}" for n in names[:5])
        if len(names) > 5:
            body += f"\n+ ... ({len(names) - 5} more)"
        try:
            self.p.vibrate(120)
        except Exception:
            pass
        dialog.alert(self.p, self.pal, "NEW BADGE",
                     body, accent=self.pal.green)

    def _action_sessions(self):
        rows = list_all(self.sessions_dir)
        if not rows:
            dialog.alert(self.p, self.pal, "SESSIONS",
                         "No sessions yet.\nStart a scan first.")
            return
        items = []
        for path, st in rows[:24]:
            icon = {"ok": "v", "pending": "^", "error": "x"}[st]
            label = f"{icon} {path.name}"
            items.append(menu.MenuItem(label, action=lambda p=path: self._show_session(p),
                                       badge=st))
        menu.run(self.p, self.pal, "SESSIONS", items)

    def _show_session(self, path: Path):
        size_kb = path.stat().st_size // 1024
        n_rows = max(0, sum(1 for _ in path.open()) - 2)
        marker = ""
        for suffix, label in ((".uploaded", "uploaded"), (".error", "error")):
            mp = path.with_suffix(path.suffix + suffix)
            if mp.exists():
                marker = f"\n{label}: {mp.read_text()[:60]}"
                break
        dialog.alert(self.p, self.pal, path.name,
                     f"{n_rows} rows  {size_kb} KB{marker}")

    def _action_config(self):
        def build_items():
            current = self.cfg.get("api_key", "")
            masked = _mask_key(current)
            mgr = idle.get()
            br = mgr.brightness if mgr else self.cfg.get("ui", {}).get("brightness", 70)
            it_s = int(mgr.timeout) if mgr else self.cfg.get("ui", {}).get("idle_timeout_s", 20)
            dim = mgr.dim_level if mgr else self.cfg.get("ui", {}).get("auto_dim_level", 10)
            gps_cfg = self.cfg.get("gps", {})
            gps_devs = gps_cfg.get("devices", [])
            gps_current = gps_devs[0] if gps_devs else "AUTO"
            gps_baud = gps_cfg.get("baud", 9600)
            return [
                menu.MenuItem(f"API KEY  [{masked}]", action=lambda: self._cfg_view_key()),
                menu.MenuItem("EDIT API KEY", action=lambda: self._cfg_edit_key()),
                menu.MenuItem("TEST CONNECTION", action=lambda: self._cfg_test()),
                menu.MenuItem("GPS DEVICE", action=lambda: self._cfg_gps_device(),
                              badge=gps_current.replace("/dev/", "")),
                menu.MenuItem("GPS BAUD", action=lambda: self._cfg_gps_baud(),
                              badge=str(gps_baud)),
                menu.MenuItem("BRIGHTNESS +", action=lambda: self._cfg_brightness(+10),
                              badge=f"{br}%"),
                menu.MenuItem("BRIGHTNESS -", action=lambda: self._cfg_brightness(-10),
                              badge=f"{br}%"),
                menu.MenuItem("IDLE TIMEOUT +", action=lambda: self._cfg_idle(+10),
                              badge=f"{it_s}s"),
                menu.MenuItem("IDLE TIMEOUT -", action=lambda: self._cfg_idle(-10),
                              badge=f"{it_s}s"),
                menu.MenuItem("DIM LEVEL +", action=lambda: self._cfg_dim(+5),
                              badge=f"{dim}%"),
                menu.MenuItem("DIM LEVEL -", action=lambda: self._cfg_dim(-5),
                              badge=f"{dim}%"),
                menu.MenuItem("BACK", action=lambda: "back"),
            ]
        menu.run(self.p, self.pal, "CONFIG", build_items)

    def _cfg_gps_device(self):
        """Pick a specific /dev/ttyACM* or /dev/ttyUSB* (or AUTO). Saves
        choice to config and hot-restarts the GPS reader so the new device
        takes effect without restarting the payload."""
        import glob as _glob
        present = sorted(_glob.glob("/dev/ttyACM*") + _glob.glob("/dev/ttyUSB*"))
        if not present:
            dialog.alert(self.p, self.pal, "GPS",
                         "No ttyACM / ttyUSB\ndevices present.\nPlug in GPS first.",
                         accent=self.pal.amber)
            return
        items = [menu.MenuItem("AUTO", action=lambda: self._set_gps_device(None))]
        for d in present:
            items.append(menu.MenuItem(d, action=lambda dev=d: self._set_gps_device(dev)))
        items.append(menu.MenuItem("BACK", action=lambda: "back"))
        menu.run(self.p, self.pal, "GPS DEVICE", items)

    def _set_gps_device(self, dev):
        gps_cfg = self.cfg.setdefault("gps", {})
        if dev is None:
            gps_cfg["devices"] = []
            label = "AUTO"
        else:
            # Keep the chosen device first; retain others as fallback order.
            others = [d for d in gps_cfg.get("devices", []) if d != dev]
            gps_cfg["devices"] = [dev] + others
            label = dev
        save_config(self.cfg)
        self._restart_gps()
        dialog.alert(self.p, self.pal, "GPS DEVICE",
                     f"Set to:\n{label}\n\nRe-locking...",
                     accent=self.pal.cyan)
        return "back"

    def _cfg_gps_baud(self):
        choices = [4800, 9600, 19200, 38400, 57600, 115200]
        cur = self.cfg.get("gps", {}).get("baud", 9600)
        try:
            idx = choices.index(cur)
        except ValueError:
            idx = 1
        new = choices[(idx + 1) % len(choices)]
        self.cfg.setdefault("gps", {})["baud"] = new
        save_config(self.cfg)
        self._restart_gps()

    def _restart_gps(self):
        try:
            self.gps.stop()
        except Exception:
            pass
        gps_cfg = self.cfg.get("gps", {})
        self.gps = GpsReader(
            gps_cfg.get("devices", []) or [],
            baud=gps_cfg.get("baud", 9600),
            min_sats=gps_cfg.get("min_sats", 4),
        )
        self.gps.start()

    def _cfg_view_key(self):
        cur = self.cfg.get("api_key", "")
        msg = f"len: {len(cur)}\n{_mask_key(cur, length=12)}\n\nEdit via SSH or use\nEDIT API KEY menu."
        dialog.alert(self.p, self.pal, "API KEY", msg, accent=self.pal.cyan)

    def _cfg_edit_key(self):
        new = keyboard.edit(self.p, self.pal, initial=self.cfg.get("api_key", ""))
        if new is None:
            return
        self.cfg["api_key"] = new
        save_config(self.cfg)
        dialog.alert(self.p, self.pal, "API KEY",
                     f"Saved {len(new)} chars.", accent=self.pal.green)

    def _cfg_test(self):
        key = self.cfg.get("api_key", "").strip()
        if not key:
            dialog.alert(self.p, self.pal, "TEST",
                         "No API key set.", accent=self.pal.red)
            return
        prog = dialog.Progress(self.p, self.pal, "TEST CONNECTION")
        prog.set(0.4, "GET /api/me ...", self.pal.fg)
        res = api.me(key)
        prog.set(1.0, f"http {res.status}", self.pal.green if res.ok else self.pal.red)
        if res.ok:
            msg = (f"user: {res.username}\n"
                   f"wifi: {res.wifi}  ble: {res.ble}\n"
                   f"total: {res.total}\n"
                   f"gang: {res.gang or '-'}")
            dialog.alert(self.p, self.pal, "TEST OK", msg, accent=self.pal.green)
        else:
            dialog.alert(self.p, self.pal, "TEST FAIL",
                         f"http {res.status}\n{res.error or ''}",
                         accent=self.pal.red)

    def _cfg_brightness(self, delta: int):
        mgr = idle.get()
        cur = mgr.brightness if mgr else self.cfg.get("ui", {}).get("brightness", 70)
        new = max(5, min(100, cur + delta))
        if mgr:
            mgr.set_brightness(new)
        else:
            try:
                self.p.set_brightness(new)
            except Exception:
                pass
        self.cfg.setdefault("ui", {})["brightness"] = new
        save_config(self.cfg)

    def _cfg_idle(self, delta: int):
        mgr = idle.get()
        cur = int(mgr.timeout) if mgr else self.cfg.get("ui", {}).get("idle_timeout_s", 20)
        new = max(5, min(600, cur + delta))
        if mgr:
            mgr.set_timeout(new)
        self.cfg.setdefault("ui", {})["idle_timeout_s"] = new
        save_config(self.cfg)

    def _cfg_dim(self, delta: int):
        mgr = idle.get()
        cur = mgr.dim_level if mgr else self.cfg.get("ui", {}).get("auto_dim_level", 10)
        new = max(0, min(100, cur + delta))
        if mgr:
            mgr.set_dim_level(new)
        self.cfg.setdefault("ui", {})["auto_dim_level"] = new
        save_config(self.cfg)

    def _action_exit(self):
        if dialog.confirm(self.p, self.pal, "POWER OFF",
                          "Quit WDGoWars Wardriver?"):
            return "exit"
        return None


def _mask_key(key: str, length: int = 8) -> str:
    if not key:
        return "(empty)"
    if len(key) <= length:
        return key
    half = max(2, length // 2)
    return f"{key[:half]}...{key[-half:]}"


if __name__ == "__main__":
    sys.exit(main())
