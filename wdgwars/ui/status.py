"""Live scan HUD — 2x2 grid: WIFI / BLE / GPS / QUEUE."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .theme import (
    Palette, clear_bg, draw_header, draw_footer, draw_panel,
    FONT_BODY, FONT_HINT, FONT_HUGE, HEADER_H, FOOTER_H,
)


@dataclass
class HudState:
    wifi_new: int = 0
    wifi_total: int = 0
    ble_new: int = 0
    ble_total: int = 0
    gps_fix: bool = False
    gps_sats: int = 0
    lat: float = 0.0
    lon: float = 0.0
    queue_rows: int = 0
    session_id: str = "----"
    paused: bool = False
    rssi_window: list[int] = field(default_factory=list)


class HudResult:
    PAUSE = "pause"
    END = "end"


def render(p, pal: Palette, st: HudState) -> None:
    clear_bg(p, pal)
    draw_header(p, pal, "LIVE SCAN", st.session_id)

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
               "QUEUE", True)

    # WIFI cell — big "new" counter, smaller "total"
    p.draw_text(14, grid_top + 22, f"{st.wifi_new:>3d}", pal.cyan, FONT_HUGE)
    p.draw_text(14, grid_top + 50, f"{st.wifi_total} total", pal.fg_dim, FONT_HINT)

    # BLE cell
    p.draw_text(midx + 12, grid_top + 22, f"{st.ble_new:>3d}", pal.magenta, FONT_HUGE)
    p.draw_text(midx + 12, grid_top + 50, f"{st.ble_total} total", pal.fg_dim, FONT_HINT)

    # GPS cell
    if st.gps_fix:
        p.draw_text(14, midy + 18, f"FIX:{st.gps_sats}", pal.green, FONT_BODY)
        p.draw_text(14, midy + 38, f"{st.lat:.4f}", pal.fg, FONT_HINT)
        p.draw_text(14, midy + 50, f"{st.lon:.4f}", pal.fg, FONT_HINT)
    else:
        p.draw_text(14, midy + 18, "NO FIX", pal.red, FONT_BODY)
        p.draw_text(14, midy + 38, f"sats:{st.gps_sats}", pal.fg_dim, FONT_HINT)

    # QUEUE cell
    rows = st.queue_rows
    rows_txt = f"{rows}" if rows < 1000 else f"{rows / 1000:.1f}k"
    p.draw_text(midx + 12, midy + 18, rows_txt, pal.amber, FONT_HUGE)
    state_txt = "PAUSED" if st.paused else "RUN"
    color = pal.amber if st.paused else pal.green
    p.draw_text(midx + 12, midy + 50, state_txt, color, FONT_HINT)

    # RSSI sparkline overlaid on WIFI panel bottom
    spark_x = 14
    spark_y = midy - 12
    spark_w = midx - 22
    if spark_w > 30 and st.rssi_window:
        _sparkline(p, pal, spark_x, spark_y, spark_w, 8, st.rssi_window)

    draw_footer(p, pal, [("A", "pause" if not st.paused else "resume"),
                          ("B", "end"), ("UP/DN", "bright")])


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


def _sparkline(p, pal: Palette, x: int, y: int, w: int, h: int, samples: list[int]) -> None:
    if not samples:
        return
    lo = min(samples + [-100])
    hi = max(samples + [-30])
    rng = max(1, hi - lo)
    n = min(len(samples), w)
    samples = samples[-n:]
    for i, s in enumerate(samples):
        v = (s - lo) / rng
        bar_h = max(1, int(v * h))
        p.vline(x + i, y + (h - bar_h), bar_h, pal.cyan)
