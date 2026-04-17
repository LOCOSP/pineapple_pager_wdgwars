"""Cyber-cyan palette and reusable drawing helpers for the WDGoWars payload."""

from __future__ import annotations

import os

# Path is relative to PAYLOAD_DIR (set in payload.sh). Falls back to the module
# directory if the env var is missing so local unit tests still work.
_BG_PATH = os.path.join(
    os.environ.get("WDGWARS_PAYLOAD_DIR") or
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "background.png",
)
_bg_handle = None
_bg_tried = False

# Bitmap font scale (pagerctl FONT_SMALL=1, FONT_MEDIUM=2, FONT_LARGE=3).
# Uniform scale — everything except small footer hints / chips renders at
# FONT_BODY=2 so the whole UI reads at one density.
FONT_HINT = 1    # footer hints, chip labels — must stay dense
FONT_BODY = 2    # menu items, dialog body, alerts, progress log
FONT_OUTPUT = 2  # alias kept so existing `size=FONT_OUTPUT` call-sites compile
FONT_TITLE = 2   # screen-title text in the header bar
FONT_HUGE = 3    # splash logo, big "%" in progress (the only size-3 surface)

CHAR_W = 6       # width of a single bitmap glyph at size=1
CHAR_H = 7


class Palette:
    def __init__(self, p):
        self.bg        = p.hex_color(0x05060A)
        self.bg_dim    = p.hex_color(0x0A0E14)
        self.cyan      = p.hex_color(0x00FFE5)
        self.cyan_dim  = p.hex_color(0x0A4F4A)
        self.magenta   = p.hex_color(0xFF2E88)
        self.green     = p.hex_color(0x39FF14)
        self.amber     = p.hex_color(0xFFB000)
        self.red       = p.hex_color(0xFF2A2A)
        self.fg        = p.hex_color(0xE6FFFB)
        self.fg_dim    = p.hex_color(0x8AA4A0)
        self.grid      = p.hex_color(0x031A19)


def clear_bg(p, pal: Palette) -> None:
    """Clear to background colour, blit the cyberpunk bg image if available,
    and lay the neon scan-lines over the top."""
    p.clear(pal.bg)
    global _bg_handle, _bg_tried
    if _bg_handle is None and not _bg_tried:
        _bg_tried = True
        if os.path.isfile(_BG_PATH):
            try:
                _bg_handle = p.load_image(_BG_PATH)
            except Exception:
                _bg_handle = None
    if _bg_handle is not None:
        try:
            # If the image dims match the screen we blit 1:1; otherwise scale.
            p.draw_image_scaled(0, 0, p.width, p.height, _bg_handle)
        except Exception:
            pass
    draw_scanlines(p, pal)


def draw_scanlines(p, pal: Palette, step: int = 4) -> None:
    for y in range(0, p.height, step):
        p.hline(0, y, p.width, pal.bg_dim)


def draw_panel(p, pal: Palette, x: int, y: int, w: int, h: int,
               title: str | None = None, active: bool = True) -> None:
    color = pal.cyan if active else pal.cyan_dim
    p.rect(x, y, w, h, color)
    p.hline(x + 2, y + 2, w - 4, pal.bg_dim)
    if title:
        chip = f"[ {title} ]"
        cw = p.text_width(chip, FONT_HINT)
        cx = x + 8
        p.fill_rect(cx - 2, y - 4, cw + 4, 9, pal.bg)
        p.draw_text(cx, y - 3, chip, color, FONT_HINT)


HEADER_H = 28   # taller to fit FONT_TITLE=2
FOOTER_H = 14


def draw_header(p, pal: Palette, title: str, sub: str | None = None) -> None:
    p.fill_rect(0, 0, p.width, HEADER_H, pal.bg_dim)
    p.hline(0, HEADER_H, p.width, pal.cyan)
    p.draw_text(6, 6, "// WDGWARS", pal.cyan, FONT_HINT)
    tw = p.text_width(title, FONT_TITLE)
    p.draw_text(p.width - tw - 6, 6, title, pal.fg, FONT_TITLE)
    if sub:
        p.draw_text(6, 17, sub, pal.fg_dim, FONT_HINT)


def draw_footer(p, pal: Palette, hints: list[tuple[str, str]]) -> None:
    y = p.height - FOOTER_H + 2
    p.hline(0, y - 3, p.width, pal.cyan_dim)
    x = 6
    for key, label in hints:
        kw = p.text_width(key, FONT_HINT)
        p.fill_rect(x - 2, y - 1, kw + 4, 10, pal.cyan)
        p.draw_text(x, y, key, pal.bg, FONT_HINT)
        x += kw + 6
        lw = p.text_width(label, FONT_HINT)
        p.draw_text(x, y, label, pal.fg_dim, FONT_HINT)
        x += lw + 10


def draw_marquee(p, pal: Palette, x: int, y: int, w: int, value: float, color: int | None = None) -> None:
    """Draw a horizontal progress bar 0..1."""
    color = color if color is not None else pal.cyan
    value = max(0.0, min(1.0, value))
    p.rect(x, y, w, 7, pal.cyan_dim)
    fill = int((w - 4) * value)
    if fill > 0:
        p.fill_rect(x + 2, y + 2, fill, 3, color)


def draw_corner(p, pal: Palette, x: int, y: int, w: int, h: int, size: int = 4) -> None:
    """Decorative corner brackets like [ ]."""
    c = pal.cyan
    p.hline(x, y, size, c); p.vline(x, y, size, c)
    p.hline(x + w - size, y, size, c); p.vline(x + w - 1, y, size, c)
    p.hline(x, y + h - 1, size, c); p.vline(x, y + h - size, size, c)
    p.hline(x + w - size, y + h - 1, size, c); p.vline(x + w - 1, y + h - size, size, c)
