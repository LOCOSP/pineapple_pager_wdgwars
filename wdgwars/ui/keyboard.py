"""On-screen hex keyboard for entering the 64-char wdgwars API key.

Layout (4x6 grid):
    0 1 2 3 4 5
    6 7 8 9 A B
    C D E F < ✓
"""

from __future__ import annotations

from . import idle
from .theme import (
    Palette, clear_bg, draw_header, draw_footer,
    FONT_BODY, FONT_HINT, HEADER_H, FOOTER_H,
)

KEYS = [
    "0", "1", "2", "3", "4", "5",
    "6", "7", "8", "9", "a", "b",
    "c", "d", "e", "f", "<", "OK",
]
COLS = 6
ROWS = 3


def edit(p, pal: Palette, initial: str = "", title: str = "API KEY") -> str | None:
    """Show hex keyboard, return new value on OK or None on B."""
    buf = list(c for c in initial.lower() if c in "0123456789abcdef")
    sel = 0

    while True:
        clear_bg(p, pal)
        draw_header(p, pal, title, f"{len(buf)}/64 hex chars")

        # Echo line (masked center, real edges)
        echo = _mask(buf)
        p.draw_text(8, HEADER_H + 6, echo, pal.cyan, FONT_HINT)

        kx0 = 8
        ky0 = HEADER_H + 22
        kw = (p.width - 16) // COLS
        avail_h = p.height - ky0 - FOOTER_H - 6
        kh = max(28, avail_h // ROWS)
        for i, ch in enumerate(KEYS):
            r, c = divmod(i, COLS)
            x = kx0 + c * kw
            y = ky0 + r * kh
            is_sel = (i == sel)
            bg = pal.cyan if is_sel else pal.bg_dim
            fg = pal.bg if is_sel else pal.cyan
            p.fill_rect(x + 1, y + 1, kw - 2, kh - 2, bg)
            p.rect(x, y, kw, kh, pal.cyan_dim)
            label = ch.upper()
            font = FONT_BODY if len(label) <= 2 else FONT_HINT
            tw = p.text_width(label, font)
            th = 7 * font
            p.draw_text(x + (kw - tw) // 2, y + (kh - th) // 2, label, fg, font)

        draw_footer(p, pal, [("A", "press"), ("B", "cancel"), ("UP/DN/L/R", "move")])
        p.flip()

        ev = idle.wait_button(p)
        if ev is None:
            continue
        if ev == p.BTN_LEFT:
            sel = (sel - 1) % len(KEYS)
        elif ev == p.BTN_RIGHT:
            sel = (sel + 1) % len(KEYS)
        elif ev == p.BTN_UP:
            sel = (sel - COLS) % len(KEYS)
        elif ev == p.BTN_DOWN:
            sel = (sel + COLS) % len(KEYS)
        elif ev == p.BTN_A:
            ch = KEYS[sel]
            if ch == "<":
                if buf:
                    buf.pop()
            elif ch == "OK":
                return "".join(buf)
            else:
                if len(buf) < 64:
                    buf.append(ch)
        elif ev == p.BTN_B:
            return None


def _mask(buf: list[str]) -> str:
    if not buf:
        return "(empty)"
    if len(buf) <= 12:
        return "".join(buf)
    return f"{''.join(buf[:6])}...{''.join(buf[-4:])}  ({len(buf)})"
