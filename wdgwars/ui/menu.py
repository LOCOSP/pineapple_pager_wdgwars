"""Vertical list menu — UP/DOWN navigate, A select, B back."""

from __future__ import annotations

from typing import Callable, Sequence

from . import idle
from .theme import (
    Palette, clear_bg, draw_header, draw_footer,
    FONT_BODY, FONT_HINT, HEADER_H, FOOTER_H,
)

# Sentinel returned when the user pressed B to back out (so callers can
# distinguish "user wants out" from "the selected action just returned None").
BACK = object()


class MenuItem:
    __slots__ = ("label", "action", "badge", "disabled")

    def __init__(self, label: str, action: Callable[[], object] | None = None,
                 badge: str | None = None, disabled: bool = False) -> None:
        self.label = label
        self.action = action
        self.badge = badge
        self.disabled = disabled


def run(p, pal: Palette, title: str,
        items: Sequence[MenuItem] | Callable[[], Sequence[MenuItem]],
        on_back: Callable[[], None] | None = None) -> object | None:
    """Draw the menu and run the input loop until BACK or an action returns
    a non-None result.

    `items` may be a sequence OR a callable that returns one each render —
    pass a callable when an item's label/badge depends on mutable state
    (e.g. current brightness %) so it refreshes between key presses without
    losing the user's row selection.
    """
    sel = 0
    row_h = 26
    while True:
        if callable(items):
            current = list(items())
        else:
            current = list(items)
        if not current:
            return BACK
        # Clamp selection in case the items list shrank between renders.
        if sel >= len(current):
            sel = len(current) - 1

        clear_bg(p, pal)
        draw_header(p, pal, title)

        # Page items so the selection always stays in view (long menus scroll).
        list_y = HEADER_H + 8
        avail_h = p.height - list_y - FOOTER_H - 4
        per_page = max(3, avail_h // row_h)
        page_start = (sel // per_page) * per_page
        page_end = min(len(current), page_start + per_page)

        for idx, i in enumerate(range(page_start, page_end)):
            it = current[i]
            y = list_y + idx * row_h
            is_sel = (i == sel)
            if is_sel:
                p.fill_rect(4, y - 2, p.width - 8, row_h - 2, pal.cyan_dim)
                p.rect(4, y - 2, p.width - 8, row_h - 2, pal.cyan)
            marker = ">" if is_sel else " "
            color = pal.fg if not it.disabled else pal.fg_dim
            if is_sel:
                color = pal.cyan
            p.draw_text(10, y + 4, marker, pal.cyan, FONT_BODY)
            p.draw_text(28, y + 4, it.label, color, FONT_BODY)
            if it.badge:
                bw = p.text_width(it.badge, FONT_HINT)
                p.draw_text(p.width - bw - 12, y + 8, it.badge, pal.amber, FONT_HINT)

        # Tiny scroll indicator if menu has more pages
        if len(current) > per_page:
            tot = (len(current) + per_page - 1) // per_page
            cur = (sel // per_page) + 1
            ind = f"{cur}/{tot}"
            iw = p.text_width(ind, FONT_HINT)
            p.draw_text(p.width - iw - 6, list_y - 8, ind, pal.fg_dim, FONT_HINT)

        draw_footer(p, pal, [("A", "ok"), ("B", "back"), ("UP/DN", "move")])
        p.flip()

        ev = idle.wait_button(p)
        if ev is None:
            continue  # wake-only press, redraw
        if ev == p.BTN_UP:
            sel = (sel - 1) % len(current)
        elif ev == p.BTN_DOWN:
            sel = (sel + 1) % len(current)
        elif ev == p.BTN_A:
            it = current[sel]
            if it.disabled or it.action is None:
                continue
            result = it.action()
            # If the action returned anything meaningful, propagate it up so
            # the caller can react (e.g. "exit" sentinel). If it returned
            # None we stay in the same menu, on the same row — useful for
            # adjusters like BRIGHTNESS +/- where the user wants to press
            # repeatedly without having to scroll back down each time.
            if result is not None:
                return result
        elif ev == p.BTN_B:
            if on_back:
                on_back()
            return BACK
