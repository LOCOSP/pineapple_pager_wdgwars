"""Entry point for the WDGoWars Wardriver payload.

Boot sequence: load config -> init Pager -> splash -> main menu loop.
"""

from __future__ import annotations

import json
import sys
import time
from collections import deque
from pathlib import Path

# Allow `python3 wdgwars.py` to find the bundled `lib/pagerctl.py` even when
# PYTHONPATH was not set by payload.sh (useful for local debugging).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "lib"))
sys.path.insert(0, str(_HERE))

from pagerctl import Pager  # noqa: E402

from ui import theme, splash, menu, dialog, status as hud, keyboard, idle  # noqa: E402
from scanners.wifi import WifiScanner, DEFAULT_PLAN  # noqa: E402
from scanners.monitor import MonitorScanner  # noqa: E402
from scanners.ble import BleScanner  # noqa: E402
from scanners.gps import GpsReader  # noqa: E402
from scanners.iface import list_interfaces, pick_wifi_source  # noqa: E402
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
                menu.MenuItem("UPLOAD LOG", action=lambda: self._action_history()),
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

    def _start_wifi(self, scan_cfg: dict):
        """Bring up the best available WiFi capture backend.

        Returns `(scanner, label)`. Honours `scan.wifi_iface` — "auto" prefers
        a live monitor interface (the Pager's own `wlan1mon`, or an external
        adapter such as an AWUS036ACM staged as `wlan2mon`) and falls back to
        `iw scan` on the best managed interface that is not the pager's own
        management radio.
        """
        mode, iface, why = pick_wifi_source(scan_cfg.get("wifi_iface", "auto"))

        if mode == "monitor":
            scanner = MonitorScanner(
                iface,
                hop=bool(scan_cfg.get("monitor_hop", True)),
                dwell_ms=int(scan_cfg.get("monitor_dwell_ms", 250)),
                emit_interval_s=float(scan_cfg.get("emit_interval_s", 1.0)),
            )
            scanner.start()
            if scanner.available:
                return scanner, f"{iface} mon"
            # Monitor was picked but could not run — say why, then degrade
            # rather than silently logging nothing for the whole drive.
            dialog.alert(self.p, self.pal, "WIFI",
                         f"Monitor capture failed:\n{scanner.last_error}\n\n"
                         f"Falling back to iw scan.",
                         accent=self.pal.amber)
            scanner.stop()
            mode, iface, why = pick_wifi_source("scan")

        scanner = WifiScanner(
            iface,
            interval_s=float(scan_cfg.get("wifi_interval_s", 0)),
            plan=scan_cfg.get("band_plan") or None,
            flush_cache=bool(scan_cfg.get("flush_bss_cache", True)),
        )
        scanner.start()
        return scanner, f"{iface} iw"

    def _live_scan(self, use_wifi: bool = True, use_ble: bool = True) -> None:
        scan_cfg = self.cfg.get("scan", {})
        store_cfg = self.cfg.get("storage", {})

        wifi = ble = None
        src_label = ""
        if use_wifi:
            wifi, src_label = self._start_wifi(scan_cfg)
        if use_ble:
            ble = BleScanner("hci0",
                             interval_s=scan_cfg.get("ble_interval_s", 12),
                             emit_interval_s=float(scan_cfg.get("emit_interval_s", 1.0)))
            ble.start()

        if wifi and wifi.last_error and not getattr(wifi, "available", True):
            dialog.alert(self.p, self.pal, "WIFI",
                         f"WiFi disabled:\n{wifi.last_error}", accent=self.pal.amber)
        if ble and not ble.available:
            dialog.alert(self.p, self.pal, "BLE",
                         f"BLE disabled:\n{ble.last_error or 'hci0 missing'}",
                         accent=self.pal.amber)

        sess = Session(
            self.sessions_dir,
            max_file_mb=store_cfg.get("max_file_mb", 30),
            refresh_ttl_s=scan_cfg.get("refresh_ttl_s", 300),
            min_move_m=scan_cfg.get("min_move_m", 30),
            rssi_delta_db=scan_cfg.get("rssi_delta_db", 6),
            require_fix=bool(scan_cfg.get("require_fix", True)),
            flush_interval_s=float(store_cfg.get("flush_interval_s", 2.0)),
        )
        st = hud.HudState(session_id=sess.session_id, source=src_label)

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

        gps_state = self.gps.state
        hud_interval = max(0.1, int(scan_cfg.get("hud_interval_ms", 500)) / 1000.0)
        tick_ms = int(scan_cfg.get("poll_interval_ms", 150))
        # Rolling window of (monotonic, rows) so the HUD can show how fast the
        # CSV is really growing — the number the bug report was missing.
        rate_hist: deque = deque(maxlen=64)
        last_render = 0.0
        last_sig = None

        try:
            while True:
                now = time.monotonic()
                live = gps_state.snapshot()
                st.gps_fix = live.fix_3d
                st.gps_sats = live.sats
                st.lat = live.lat
                st.lon = live.lon

                if st.paused:
                    # Keep draining or the queues grow without bound and the
                    # whole backlog lands on the CSV at the *resume* position.
                    if wifi:
                        wifi.drain()
                    if ble:
                        ble.drain()
                else:
                    if wifi:
                        for obs in wifi.drain():
                            st.wifi_seen += 1
                            st.rssi_window.append(obs.rssi)
                            # Geo-tag against where we were when the frame was
                            # heard, not where we are now that it is written.
                            if sess.add_wifi(obs, gps_state.at(obs.first_seen)):
                                st.wifi_rows += 1
                    if ble:
                        for obs in ble.drain():
                            st.ble_seen += 1
                            if sess.add_ble(obs, gps_state.at(obs.first_seen)):
                                st.ble_rows += 1

                    st.total_rows = sess.stats.rows_written
                    st.skipped_no_fix = sess.stats.skipped_no_fix
                    if not rate_hist or (now - rate_hist[-1][0]) >= 1.0:
                        rate_hist.append((now, st.total_rows))
                    st.rows_per_min = _rows_per_min(rate_hist)

                warn = getattr(wifi, "last_error", None) if wifi else None
                st.warn = warn or ""

                # Render & flip only when the backlight is on and something
                # actually changed; a full redraw is ~56 hlines plus a scaled
                # background blit, which is real money on a 580 MHz core.
                mgr = idle.get()
                asleep = mgr.tick() if mgr else False
                if not asleep and (now - last_render) >= hud_interval:
                    sig = st.signature()
                    if sig != last_sig or (now - last_render) >= 2.0:
                        hud.render(self.p, self.pal, st)
                        self.p.flip()
                        last_sig = sig
                    last_render = now

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
                                if st.paused:
                                    sess.flush()
                                last_sig = None
                            elif btn == self.p.BTN_B:
                                sess.flush()
                                if dialog.confirm(self.p, self.pal, "END SESSION",
                                                  f"Stop scan and save\n{sess.stats.rows_written} rows?"):
                                    break
                                last_sig = None
                            elif btn == self.p.BTN_UP:
                                adjust_brightness(+10)
                            elif btn == self.p.BTN_DOWN:
                                adjust_brightness(-10)

                self.p.delay(tick_ms)
        finally:
            if wifi:
                wifi.stop()
            if ble:
                ble.stop()
            # One last drain: the scanners were still emitting up to the
            # moment we tore them down.
            try:
                if wifi:
                    for obs in wifi.drain():
                        if sess.add_wifi(obs, gps_state.at(obs.first_seen)):
                            st.wifi_rows += 1
                if ble:
                    for obs in ble.drain():
                        if sess.add_ble(obs, gps_state.at(obs.first_seen)):
                            st.ble_rows += 1
            except Exception:
                pass
            sess.close()

        s = sess.stats
        held = f"\nno-fix skipped: {s.skipped_no_fix}" if s.skipped_no_fix else ""
        dialog.alert(self.p, self.pal, "SAVED",
                     f"Wrote {s.rows_written} rows\n"
                     f"WiFi: {s.wifi_total}  BLE: {s.ble_total}\n"
                     f"File: {Path(s.files[-1]).name}{held}",
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

        upload_mode = self.cfg.get("upload", {}).get("mode", "auto")
        for i, csv in enumerate(pending):
            prog.set(i / total, f"-> {csv.name} ({csv.stat().st_size // 1024}k)", self.pal.fg)
            res = api.upload_with_retry(
                api_key, csv, mode=upload_mode,
                on_attempt=lambda a, msg: prog.set(i / total, msg, self.pal.fg_dim))
            if res.ok:
                mark_uploaded(csv, res.body)
                merged_total += res.merged_samples
                prog.set((i + 1) / total,
                         f"[OK] {csv.name}  {res.summary()}", self.pal.green)
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

    def _action_history(self):
        """Server-side view of past uploads — the only place the pager can see
        how many rows the backend actually took, and how many it threw away
        for missing GPS or bad formatting."""
        api_key = self.cfg.get("api_key", "").strip()
        if not api_key:
            dialog.alert(self.p, self.pal, "UPLOAD LOG",
                         "No API key configured.\nGo to CONFIG.",
                         accent=self.pal.red)
            return
        prog = dialog.Progress(self.p, self.pal, "UPLOAD LOG")
        prog.set(0.5, "GET /api/upload-history ...", self.pal.fg)
        res = api.upload_history(api_key, limit=20)
        prog.set(1.0, f"http {res.status}",
                 self.pal.green if res.ok else self.pal.red)
        if not res.ok:
            dialog.alert(self.p, self.pal, "UPLOAD LOG",
                         f"http {res.status}\n{res.error or ''}",
                         accent=self.pal.red)
            return
        if not res.uploads:
            dialog.alert(self.p, self.pal, "UPLOAD LOG",
                         "No uploads recorded\nfor this key yet.",
                         accent=self.pal.cyan)
            return

        items = []
        for entry in res.uploads:
            got = entry.result.get("imported", entry.result.get("captured", 0))
            items.append(menu.MenuItem(
                entry.created_at[5:16] or entry.filename[:16],
                action=lambda e=entry: self._show_history_entry(e),
                badge=str(got)))
        items.append(menu.MenuItem("BACK", action=lambda: "back"))
        menu.run(self.p, self.pal, "UPLOAD LOG", items)

    def _show_history_entry(self, entry):
        r = entry.result
        lines = [entry.filename[:28],
                 f"{entry.file_size // 1024} KB via {entry.endpoint}",
                 entry.created_at, ""]
        for key in ("imported", "captured", "updated", "duplicates",
                    "no_gps", "bad_rows", "cooldown"):
            if key in r:
                lines.append(f"{key}: {r[key]}")
        accent = self.pal.amber if r.get("no_gps") or r.get("bad_rows") \
            else self.pal.green
        dialog.alert(self.p, self.pal, "UPLOAD", "\n".join(lines), accent=accent)

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
        n_rows = _count_rows(path)
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
            scan_cfg = self.cfg.get("scan", {})
            return [
                menu.MenuItem(f"API KEY  [{masked}]", action=lambda: self._cfg_view_key()),
                menu.MenuItem("EDIT API KEY", action=lambda: self._cfg_edit_key()),
                menu.MenuItem("TEST CONNECTION", action=lambda: self._cfg_test()),
                menu.MenuItem("SCAN SETUP", action=lambda: self._cfg_scan(),
                              badge=str(scan_cfg.get("wifi_iface", "auto"))),
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

    # ---------------- scan setup ---------------- #

    # Presets are ordered fastest-cycle first. "rotate" gives a position
    # sample every ~2 s instead of every ~15 s, which is the single biggest
    # win available on the `iw scan` backend.
    BAND_PRESETS = [
        ("rotate", DEFAULT_PLAN),
        ("2.4 only", ["2g"]),
        ("2.4 + 5", ["2g", "5g_fast", "2g", "5g_dfs"]),
        ("full sweep", ["all"]),
    ]

    def _cfg_scan(self):
        def build():
            sc = self.cfg.setdefault("scan", {})
            plan = sc.get("band_plan") or DEFAULT_PLAN
            plan_name = next((n for n, p in self.BAND_PRESETS if p == plan),
                             "custom")
            return [
                menu.MenuItem("WIFI SOURCE", action=lambda: self._cfg_wifi_source(),
                              badge=str(sc.get("wifi_iface", "auto"))),
                menu.MenuItem("BAND PLAN", action=lambda: self._cfg_cycle_band(),
                              badge=plan_name),
                menu.MenuItem("MONITOR HOP", action=lambda: self._cfg_toggle("monitor_hop", True),
                              badge="on" if sc.get("monitor_hop", True) else "off"),
                menu.MenuItem("MOVE FILTER +", action=lambda: self._cfg_scan_num("min_move_m", +10, 0, 300),
                              badge=f"{sc.get('min_move_m', 30)}m"),
                menu.MenuItem("MOVE FILTER -", action=lambda: self._cfg_scan_num("min_move_m", -10, 0, 300),
                              badge=f"{sc.get('min_move_m', 30)}m"),
                menu.MenuItem("REFRESH TTL +", action=lambda: self._cfg_scan_num("refresh_ttl_s", +60, 30, 3600),
                              badge=f"{sc.get('refresh_ttl_s', 300)}s"),
                menu.MenuItem("REFRESH TTL -", action=lambda: self._cfg_scan_num("refresh_ttl_s", -60, 30, 3600),
                              badge=f"{sc.get('refresh_ttl_s', 300)}s"),
                menu.MenuItem("REQUIRE GPS FIX", action=lambda: self._cfg_toggle("require_fix", True),
                              badge="on" if sc.get("require_fix", True) else "off"),
                menu.MenuItem("BACK", action=lambda: "back"),
            ]
        menu.run(self.p, self.pal, "SCAN SETUP", build)

    def _cfg_wifi_source(self):
        """Pick the capture interface — auto, a forced backend, or a specific
        device. Detected interfaces are listed with their current mode so an
        externally-staged `wlan2mon` is one press away."""
        ifaces = list_interfaces()
        mode, chosen, why = pick_wifi_source(
            self.cfg.get("scan", {}).get("wifi_iface", "auto"), ifaces)
        items = [
            menu.MenuItem("AUTO", action=lambda: self._set_wifi_iface("auto"),
                          badge=f"{chosen}/{mode}"),
            menu.MenuItem("FORCE MONITOR", action=lambda: self._set_wifi_iface("monitor")),
            menu.MenuItem("FORCE IW SCAN", action=lambda: self._set_wifi_iface("scan")),
        ]
        for i in ifaces:
            items.append(menu.MenuItem(
                i.name, action=lambda n=i.name: self._set_wifi_iface(n),
                badge=("mon" if i.is_monitor else i.type[:6]) + ("" if i.up else " down")))
        items.append(menu.MenuItem("BACK", action=lambda: "back"))
        menu.run(self.p, self.pal, "WIFI SOURCE", items)

    def _set_wifi_iface(self, value: str):
        self.cfg.setdefault("scan", {})["wifi_iface"] = value
        save_config(self.cfg)
        mode, iface, why = pick_wifi_source(value)
        dialog.alert(self.p, self.pal, "WIFI SOURCE",
                     f"{value}\n\n-> {iface} ({mode})\n{why}",
                     accent=self.pal.cyan)
        return "back"

    def _cfg_cycle_band(self):
        sc = self.cfg.setdefault("scan", {})
        cur = sc.get("band_plan") or DEFAULT_PLAN
        names = [p for _, p in self.BAND_PRESETS]
        try:
            idx = names.index(cur)
        except ValueError:
            idx = -1
        sc["band_plan"] = names[(idx + 1) % len(names)]
        save_config(self.cfg)

    def _cfg_toggle(self, key: str, default: bool):
        sc = self.cfg.setdefault("scan", {})
        sc[key] = not bool(sc.get(key, default))
        save_config(self.cfg)

    def _cfg_scan_num(self, key: str, delta: int, lo: int, hi: int):
        sc = self.cfg.setdefault("scan", {})
        defaults = {"min_move_m": 30, "refresh_ttl_s": 300}
        cur = int(sc.get(key, defaults.get(key, 0)))
        sc[key] = max(lo, min(hi, cur + delta))
        save_config(self.cfg)

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


def _rows_per_min(hist) -> float:
    """Rows/minute over the last ~60 s of samples."""
    if len(hist) < 2:
        return 0.0
    t1, r1 = hist[-1]
    t0, r0 = hist[0]
    for t, r in hist:
        if t1 - t <= 60.0:
            t0, r0 = t, r
            break
    dt = t1 - t0
    if dt < 2.0:
        return 0.0
    return (r1 - r0) * 60.0 / dt


def _count_rows(path: Path) -> int:
    """Data rows in a session CSV, minus the two header lines.

    Chunked rather than `sum(1 for _ in open(path))` — these files rotate at
    30 MB and the SESSIONS screen used to read every byte through Python's
    line iterator just to show a count.
    """
    total = 0
    try:
        with path.open("rb") as fh:
            while True:
                block = fh.read(1 << 16)
                if not block:
                    break
                total += block.count(b"\n")
    except OSError:
        return 0
    return max(0, total - 2)


def _mask_key(key: str, length: int = 8) -> str:
    if not key:
        return "(empty)"
    if len(key) <= length:
        return key
    half = max(2, length // 2)
    return f"{key[:half]}...{key[-half:]}"


if __name__ == "__main__":
    sys.exit(main())
