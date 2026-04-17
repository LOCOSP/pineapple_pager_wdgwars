"""Splash screen with ASCII logo + boot beep."""

from __future__ import annotations

from .theme import Palette, clear_bg, draw_corner

LOGO = [
    "W   W  DDD   GGG  W   W  AAA  RRRR   SSS",
    "W   W  D  D G     W   W A   A R   R S    ",
    "W W W  D  D G  GG W W W AAAAA RRRR   SSS ",
    "WW WW  D  D G   G WW WW A   A R  R     S",
    "W   W  DDD   GGG  W   W A   A R   R SSSS ",
]

TAGLINE = "// hak5 pager wardriver  ::  wdgwars.pl"


def show(p, pal: Palette, hold_ms: int = 1100) -> None:
    clear_bg(p, pal)
    draw_corner(p, pal, 4, 4, p.width - 8, p.height - 8, 6)

    y0 = (p.height - len(LOGO) * 7) // 2 - 14
    for i, line in enumerate(LOGO):
        p.draw_text_centered(y0 + i * 7, line, pal.cyan, 1)

    p.draw_text_centered(y0 + len(LOGO) * 7 + 8, TAGLINE, pal.fg_dim, 1)
    p.draw_text_centered(p.height - 22, "[ booting ]", pal.magenta, 1)

    p.flip()
    try:
        p.vibrate(40)
        p.play_rtttl_sync("wd:d=8,o=5,b=200:c6,e6,g6", with_vibration=False)
    except Exception:
        pass
    p.delay(hold_ms)
