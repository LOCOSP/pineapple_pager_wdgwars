"""Idle backlight manager — kills the LCD backlight after N seconds of input
inactivity to keep the pager from cooking itself, restores user brightness on
the next button press.

A module-level singleton avoids threading an `idle` parameter through every UI
function. `init()` from the main entry; `wait_button()` replaces `p.wait_button()`
in any blocking input loop; polling loops should call `tick()` and consume
wake-up presses with `wake_consume()`.
"""

from __future__ import annotations

import time


class IdleManager:
    def __init__(self, p, timeout_s: float = 20.0, brightness: int = 70,
                 dim_level: int = 10) -> None:
        self.p = p
        self.timeout = float(timeout_s)
        self.brightness = max(5, min(100, int(brightness)))
        # On this hardware brightness 10 is effectively off (matches the
        # pagergotchi convention: AUTO_DIM_LEVELS = [10=off, 20, 40, ...]).
        self.dim_level = max(0, min(self.brightness, int(dim_level)))
        self.last_activity = time.time()
        self.asleep = False
        self.enabled = True

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        if not self.enabled and self.asleep:
            self._wake_backlight()
            self.asleep = False
        self.last_activity = time.time()

    def set_timeout(self, seconds: float) -> None:
        self.timeout = max(5.0, float(seconds))
        self.last_activity = time.time()

    def set_brightness(self, value: int) -> None:
        self.brightness = max(5, min(100, int(value)))
        # Keep dim_level <= brightness so we never "dim" to a brighter value.
        if self.dim_level > self.brightness:
            self.dim_level = self.brightness
        if not self.asleep:
            try:
                self.p.set_brightness(self.brightness)
            except Exception:
                pass

    def set_dim_level(self, value: int) -> None:
        self.dim_level = max(0, min(self.brightness, int(value)))
        if self.asleep:
            try:
                self.p.set_brightness(self.dim_level)
            except Exception:
                pass

    def mark_active(self) -> None:
        self.last_activity = time.time()

    def tick(self) -> bool:
        """Call from polling loops. Returns True iff backlight is currently off."""
        if not self.enabled:
            return False
        if not self.asleep and (time.time() - self.last_activity) >= self.timeout:
            self._sleep_backlight()
            self.asleep = True
        return self.asleep

    def wake_consume(self) -> bool:
        """Process an input event. Returns True if we were sleeping (caller should
        suppress action and treat the press as wake-only)."""
        if self.asleep:
            self._wake_backlight()
            self.asleep = False
            self.last_activity = time.time()
            return True
        self.last_activity = time.time()
        return False

    def _sleep_backlight(self) -> None:
        # Match pagergotchi's pattern: a single set_brightness(dim_level) call,
        # no ramp, no sysfs tricks. dim_level=10 is the "off" floor on this
        # hardware (LCD has residual emission below that, so going to 0 makes
        # no visible difference and confuses some PWM drivers).
        try:
            self.p.set_brightness(self.dim_level)
        except Exception:
            pass
        try:
            self.p.led_all_off()
        except Exception:
            pass
        _idle_log(f"SLEEP -> set({self.dim_level})")

    def _wake_backlight(self) -> None:
        try:
            self.p.set_brightness(self.brightness)
        except Exception:
            pass
        _idle_log(f"WAKE  -> set({self.brightness})")


def _idle_log(msg: str) -> None:
    try:
        with open("/tmp/wdgwars-idle.log", "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _ramp(start: int, end: int, steps: int) -> list[int]:
    if steps <= 1:
        return [end]
    return [round(start + (end - start) * i / (steps - 1)) for i in range(steps)]


_manager: IdleManager | None = None


def init(p, timeout_s: float = 20.0, brightness: int = 70,
         dim_level: int = 10) -> IdleManager:
    global _manager
    _manager = IdleManager(p, timeout_s=timeout_s, brightness=brightness,
                           dim_level=dim_level)
    return _manager


def get() -> IdleManager | None:
    return _manager


def wait_button(p, poll_ms: int = 150):
    """Drop-in for `p.wait_button()` that respects the idle timer.

    Returns the pressed button code, or `None` if the press only woke the
    screen from sleep (caller should treat as no-op and loop again).
    """
    mgr = _manager
    while True:
        if mgr:
            mgr.tick()
        if p.has_input_events():
            ev = p.get_input_event()
            if not ev:
                continue
            btn, etype, _ = ev
            if etype != getattr(p, "EVENT_PRESS", 1):
                continue
            if mgr and mgr.wake_consume():
                # Drain any remaining queued events from the wake press
                while p.has_input_events():
                    p.get_input_event()
                return None
            return btn
        p.delay(poll_ms)
