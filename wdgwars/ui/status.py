"""Live scan HUD — 2x2 grid: WIFI / BLE / GPS / ROWS.

The big number in each cell is *rows written to the CSV*, not raw sightings.
That distinction was the source of a bug report: the old HUD showed total
sightings, which climbs several times faster than the file does (every AP is
re-seen on every sweep), and it read like the writer was falling behind when
nothing was wrong. Sightings are still shown, in small type, labelled "seen".
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .theme import (
    Palette, clear_bg, draw_header, draw_footer, draw_panel,
    FONT_BODY, FONT_HINT, FONT_HUGE, HEADER_H, FOOTER_H,
)


@dataclass
class HudState:
    # rows that actually reached the CSV
    wifi_rows: int = 0
    ble_rows: int = 0
    total_rows: int = 0
    # raw sightings drained from the scanners
    wifi_seen: int = 0
    ble_seen: int = 0
    # gps
    gps_fix: bool = False
    gps_sats: int = 0
    lat: float = 0.0
    lon: float = 0.0
    skipped_no_fix: int = 0
    # misc
    rows_per_min: float = 0.0
    session_id: str = "----"
    source: str = ""
    paused: bool = False
    warn: str = ""
    rssi_window: deque = field(default_factory=lambda: deque(maxlen=64))

    def signature(self) -> tuple:
        """Cheap change-detector so the loop can skip identical redraws."""
        return (self.wifi_rows, self.ble_rows, self.total_rows,
                self.wifi_seen, self.ble_seen, self.gps_fix, self.gps_sats,
                round(self.lat, 4), round(self.lon, 4), self.skipped_no_fix,
                int(self.rows_per_min), self.paused, self.warn)


class HudResult:
    PAUSE = "pause"
    END = "end"


def render(p, pal: Palette, st: HudState) -> None:
    clear_bg(p, pal)
    sub = st.session_id if not st.source else f"{st.session_id}  {st.source}"
    draw_header(p, pal, "LIVE SCAN", sub)

    grid_top = HEADER_H + 8
    grid_bottom = p.height - FOOTER_H - 4
    midx = p.width // 2
    midy = (grid_top + grid_bottom) // 2

    # 2x2 panels
    draw_panel(p, pal, 6, grid_top + 6, midx - 10, midy - grid_top - 4, "WIFI", True)
    draw_panel(p, pal, midx + 4, grid_top + 6, midx - 10, midy - grid_top - 4, "BLE", True)
    draw_panel(p, pal, 6, midy + 4, midx - 10, grid_bottom - midy - 4,
               "GPS", st.gps_fix)
    draw_panel(p, pal, midx + 4, midy + 4, midx - 10, grid_bottom - midy - 4,
               "ROWS", True)

    # WIFI cell — rows written big, raw sightings small
    p.draw_text(14, grid_top + 22, _short(st.wifi_rows), pal.cyan, FONT_HUGE)
    p.draw_text(14, grid_top + 50, f"{st.wifi_seen} seen", pal.fg_dim, FONT_HINT)

    # BLE cell
    p.draw_text(midx + 12, grid_top + 22, _short(st.ble_rows), pal.magenta, FONT_HUGE)
    p.draw_text(midx + 12, grid_top + 50, f"{st.ble_seen} seen", pal.fg_dim, FONT_HINT)

    # GPS cell
    if st.gps_fix:
        p.draw_text(14, midy + 18, f"FIX:{st.gps_sats}", pal.green, FONT_BODY)
        p.draw_text(14, midy + 38, f"{st.lat:.4f}", pal.fg, FONT_HINT)
        p.draw_text(14, midy + 50, f"{st.lon:.4f}", pal.fg, FONT_HINT)
    else:
        p.draw_text(14, midy + 18, "NO FIX", pal.red, FONT_BODY)
        p.draw_text(14, midy + 38, f"sats:{st.gps_sats}", pal.fg_dim, FONT_HINT)
        if st.skipped_no_fix:
            p.draw_text(14, midy + 50, f"held:{st.skipped_no_fix}",
                        pal.amber, FONT_HINT)

    # ROWS cell — total written plus the rate the file is actually growing at
    p.draw_text(midx + 12, midy + 18, _short(st.total_rows), pal.amber, FONT_HUGE)
    if st.paused:
        p.draw_text(midx + 12, midy + 50, "PAUSED", pal.amber, FONT_HINT)
    else:
        p.draw_text(midx + 12, midy + 50, f"{st.rows_per_min:.0f}/min",
                    pal.green, FONT_HINT)

    # RSSI sparkline overlaid on WIFI panel bottom
    spark_x = 14
    spark_y = midy - 12
    spark_w = midx - 22
    if spark_w > 30 and st.rssi_window:
        _sparkline(p, pal, spark_x, spark_y, spark_w, 8, st.rssi_window)

    # A warning replaces the verbose hints but never the way out of the screen.
    if st.warn:
        draw_footer(p, pal, [("!", st.warn[:22]), ("B", "end")])
    else:
        draw_footer(p, pal, [("A", "pause" if not st.paused else "resume"),
                             ("B", "end"), ("UP/DN", "bright")])


def _short(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 100000:
        return f"{n / 1000:.1f}k"
    return f"{n // 1000}k"


def loop(p, pal: Palette, hud: HudState, tick_ms: int = 200,
         on_brightness: Callable[[int], None] | None = None) -> str:
    while True:
        render(p, pal, hud)
        p.flip()
        if p.has_input_events():
            ev = p.get_input_event()
            if not ev:
                continue
            btn, etype, _ = ev
            if etype != getattr(p, "EVENT_PRESS", 1):
                continue
            if btn == p.BTN_A:
                hud.paused = not hud.paused
            elif btn == p.BTN_B:
                return HudResult.END
            elif btn == p.BTN_UP and on_brightness:
                on_brightness(+10)
            elif btn == p.BTN_DOWN and on_brightness:
                on_brightness(-10)
        p.delay(tick_ms)


def _sparkline(p, pal: Palette, x: int, y: int, w: int, h: int, samples) -> None:
    if not samples:
        return
    vals = list(samples)
    lo = min(vals + [-100])
    hi = max(vals + [-30])
    rng = max(1, hi - lo)
    vals = vals[-min(len(vals), w):]
    for i, s in enumerate(vals):
        v = (s - lo) / rng
        bar_h = max(1, int(v * h))
        p.vline(x + i, y + (h - bar_h), bar_h, pal.cyan)
