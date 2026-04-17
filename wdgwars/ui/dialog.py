"""Modal dialogs: alert / confirm / progress / wait."""

from __future__ import annotations

from typing import Callable

from . import idle
from .theme import (
    Palette, clear_bg, draw_header, draw_footer, draw_marquee, draw_corner,
    FONT_BODY, FONT_HINT, FONT_HUGE, FONT_OUTPUT, HEADER_H, FOOTER_H, CHAR_W, CHAR_H,
)


def alert(p, pal: Palette, title: str, message: str, accent: int | None = None) -> None:
    color = accent if accent is not None else pal.magenta
    clear_bg(p, pal)
    draw_header(p, pal, title)
    box_y = HEADER_H + 14
    box_h = p.height - box_y - FOOTER_H - 14
    draw_corner(p, pal, 12, box_y, p.width - 24, box_h, 6)
    _multiline(p, pal, message, 22, box_y + 14, p.width - 44, color, size=FONT_OUTPUT)
    draw_footer(p, pal, [("A", "ok")])
    p.flip()
    while True:
        btn = idle.wait_button(p)
        if btn is None:
            continue
        if btn in (p.BTN_A, p.BTN_B):
            return


def confirm(p, pal: Palette, title: str, message: str) -> bool:
    clear_bg(p, pal)
    draw_header(p, pal, title)
    box_y = HEADER_H + 14
    box_h = p.height - box_y - FOOTER_H - 14
    draw_corner(p, pal, 12, box_y, p.width - 24, box_h, 6)
    _multiline(p, pal, message, 22, box_y + 14, p.width - 44, pal.fg, size=FONT_OUTPUT)
    draw_footer(p, pal, [("A", "yes"), ("B", "no")])
    p.flip()
    while True:
        ev = idle.wait_button(p)
        if ev is None:
            continue
        if ev == p.BTN_A:
            return True
        if ev == p.BTN_B:
            return False


class Progress:
    def __init__(self, p, pal: Palette, title: str) -> None:
        self.p = p
        self.pal = pal
        self.title = title
        self.lines: list[tuple[str, int]] = []
        self.pct = 0.0

    def set(self, pct: float, line: str | None = None, color: int | None = None) -> None:
        self.pct = pct
        if line is not None:
            self.lines.append((line, color if color is not None else self.pal.fg_dim))
            # Trim to the last N lines we can actually fit on screen at FONT_BODY.
            self.lines = self.lines[-6:]
        self._render()

    def _render(self) -> None:
        p, pal = self.p, self.pal
        clear_bg(p, pal)
        draw_header(p, pal, self.title)
        bar_y = HEADER_H + 8
        draw_marquee(p, pal, 16, bar_y, p.width - 32, self.pct)
        pct_txt = f"{int(self.pct * 100):>3d}%"
        p.draw_text_centered(bar_y + 14, pct_txt, pal.cyan, FONT_HUGE)
        # Log lines at FONT_OUTPUT so they match alert body size — this is the
        # data the user actively reads (TEST CONNECTION responses, SYNC status).
        font = FONT_OUTPUT
        y = bar_y + 14 + (CHAR_H + 4) * FONT_HUGE + 8
        line_h = (CHAR_H + 3) * font
        max_w = (p.width - 24) // (CHAR_W * font)
        avail = p.height - FOOTER_H - y - 4
        max_lines = max(1, avail // line_h)
        for line, color in self.lines[-max_lines:]:
            p.draw_text(12, y, line[:max_w], color, font)
            y += line_h
        draw_footer(p, pal, [("B", "close")])
        p.flip()

    def wait_dismiss(self) -> None:
        while True:
            ev = idle.wait_button(self.p)
            if ev is None:
                continue
            if ev in (self.p.BTN_A, self.p.BTN_B):
                return


def wait_with(p, pal: Palette, title: str, message: str,
              poll: Callable[[], bool], timeout_ms: int = 0,
              tick_ms: int = 250,
              live_message: Callable[[], str] | None = None) -> bool:
    """Show animated waiting screen until poll() returns True. B aborts.

    timeout_ms <= 0 means wait indefinitely.
    live_message, if given, replaces `message` on every tick (lets caller show
    changing telemetry like sat count).
    """
    elapsed = 0
    dot = 0
    while True:
        clear_bg(p, pal)
        draw_header(p, pal, title)
        box_y = HEADER_H + 14
        box_h = p.height - box_y - FOOTER_H - 32
        draw_corner(p, pal, 12, box_y, p.width - 24, box_h, 6)
        msg = live_message() if live_message else message
        _multiline(p, pal, msg, 22, box_y + 12, p.width - 44, pal.fg, size=FONT_OUTPUT)
        spinner = "[" + "." * (dot + 1) + " " * (3 - dot) + "]"
        p.draw_text_centered(box_y + box_h - 22, spinner, pal.cyan, FONT_BODY)
        elapsed_s = elapsed // 1000
        p.draw_text_centered(p.height - FOOTER_H - 12,
                             f"elapsed {elapsed_s}s", pal.fg_dim, FONT_HINT)
        draw_footer(p, pal, [("B", "abort")])
        p.flip()
        if poll():
            return True
        mgr = idle.get()
        if mgr:
            mgr.tick()
        if p.has_input_events():
            ev = p.get_input_event()
            if ev:
                # First press just wakes; don't abort the wait
                if mgr and mgr.wake_consume():
                    while p.has_input_events():
                        p.get_input_event()
                elif ev[0] == p.BTN_B:
                    return False
        p.delay(tick_ms)
        elapsed += tick_ms
        dot = (dot + 1) % 4
        if 0 < timeout_ms <= elapsed:
            return False
    return False


def _multiline(p, pal: Palette, text: str, x: int, y: int, w: int, color: int,
               size: int = FONT_BODY) -> None:
    max_chars = max(6, w // (CHAR_W * size))
    line_h = (CHAR_H + 4) * size
    cy = y
    for raw in text.split("\n"):
        words = raw.split(" ")
        cur = ""
        for word in words:
            tentative = (cur + " " + word).strip()
            if len(tentative) > max_chars and cur:
                p.draw_text(x, cy, cur, color, size)
                cy += line_h
                cur = word
            else:
                cur = tentative
        if cur:
            p.draw_text(x, cy, cur, color, size)
            cy += line_h
