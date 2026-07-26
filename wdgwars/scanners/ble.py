"""BLE scanner using `bluetoothctl` over a pseudo-tty.

bluetoothctl only emits live `[CHG] Device ... RSSI:` events when it thinks
its stdout is a terminal — when piped through a regular subprocess pipe it
silently drops async events. So we wrap it in a pty.
"""

from __future__ import annotations

import errno
import os
import pty
import queue
import re
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass
class BleObs:
    mac: str
    name: str
    rssi: int
    first_seen: float


_DEVICE_RE = re.compile(r"Device\s+([0-9A-Fa-f:]{17})\s*(.*)?")
# bluez 5.72 prints `RSSI: 0xffffffb1 (-79)`; older versions print `RSSI: -79`.
# Prefer the parenthesised signed decimal, fall back to the first signed int.
_RSSI_RE = re.compile(r"RSSI:[^(\n]*\((-?\d+)\)|RSSI:\s*(-?\d+)")
_NAME_RE = re.compile(r"Name:\s*(.+)")


class BleScanner:
    """Run bluetoothctl with a pty-less pipe and parse live updates."""

    def __init__(self, hci: str = "hci0", interval_s: float = 12.0,
                 emit_interval_s: float = 1.0, queue_max: int = 256) -> None:
        self.hci = hci
        self.interval = interval_s
        self.emit_interval_s = emit_interval_s
        self._q: queue.Queue[list[BleObs]] = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._thr: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._pending: dict[str, BleObs] = {}
        self.last_error: str | None = None
        self.available: bool = False
        self.events: int = 0
        self.dropped_batches: int = 0

    def start(self) -> None:
        if not shutil.which("bluetoothctl"):
            self.last_error = "`bluetoothctl` not installed (opkg install bluez-utils)"
            return
        if not os.path.exists(f"/sys/class/bluetooth/{self.hci}"):
            self.last_error = f"{self.hci} not present"
            return
        self.available = True
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="ble-scan", daemon=True)
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, b"scan off\nexit\n")
            except Exception:
                pass
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except Exception:
                pass
            self._master_fd = None
        if self._thr:
            self._thr.join(timeout=2)
            self._thr = None

    def drain(self) -> list[BleObs]:
        out: list[BleObs] = []
        while True:
            try:
                out.extend(self._q.get_nowait())
            except queue.Empty:
                return out

    def _flush(self) -> None:
        """Hand the current window's strongest sighting per MAC to the queue.

        `duplicate-data on` makes bluez re-emit RSSI for every advertisement,
        which in a station concourse is thousands of events a second. Emitting
        one object per MAC per second instead carries the same information —
        the closest approach — for a fraction of the allocation churn on a
        580 MHz core.
        """
        if not self._pending:
            return
        batch = list(self._pending.values())
        self._pending.clear()
        try:
            self._q.put_nowait(batch)
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(batch)
            except (queue.Empty, queue.Full):
                pass
            self.dropped_batches += 1

    def _run(self) -> None:
        # Spawn bluetoothctl under a pty so it emits async [CHG]/[NEW] events.
        try:
            master_fd, slave_fd = pty.openpty()
            self._master_fd = master_fd
            self._proc = subprocess.Popen(
                ["bluetoothctl"],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
        except Exception as e:
            self.last_error = f"spawn: {e}"
            return
        try:
            # Disable every bluez discovery filter so we see raw-like LE
            # advertising traffic instead of the heavily-deduped default.
            # Without these, bluetoothctl only emits RSSI for a fresh or
            # strongly-moving device — which is why handhelds doing raw HCI
            # (ESP32 / Marauder / Bruce) see 10× more than we do.
            for cmd in (b"power on\n",
                        b"agent off\n",
                        b"menu scan\n",
                        b"clear\n",                # reset any leftover filter
                        b"transport le\n",         # LE-only
                        b"duplicate-data on\n",    # re-emit RSSI on repeats
                        b"rssi 0\n",               # no RSSI floor
                        b"pathloss 0\n",           # no pathloss floor
                        b"back\n",
                        b"scan on\n"):
                os.write(master_fd, cmd)
                time.sleep(0.07)
        except Exception as e:
            self.last_error = f"setup: {e}"
            return

        current_mac: str | None = None
        names: dict[str, str] = {}
        buf = b""
        last_emit = time.monotonic()
        while not self._stop.is_set() and self._proc.poll() is None:
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.5)
            except (ValueError, OSError):
                break
            now = time.monotonic()
            if now - last_emit >= self.emit_interval_s:
                self._flush()
                last_emit = now
            if not ready:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, _, buf = buf.partition(b"\n")
                line = _strip_ansi(raw.decode(errors="replace")).strip()
                if not line:
                    continue
                d = _DEVICE_RE.search(line)
                if d:
                    current_mac = d.group(1).lower()
                    trailing = (d.group(2) or "").strip()
                    if trailing and not trailing.lower().startswith(("rssi", "txpower", "uuid", "manufacturer")):
                        names.setdefault(current_mac, trailing)
                n = _NAME_RE.search(line)
                if n and current_mac:
                    names[current_mac] = n.group(1).strip()
                r = _RSSI_RE.search(line)
                if r and current_mac:
                    rssi = int(r.group(1) or r.group(2))
                    self.events += 1
                    prev = self._pending.get(current_mac)
                    # Keep the strongest sighting in the window — that is the
                    # closest approach, which is what geo-locating a device
                    # actually wants.
                    if prev is None or rssi > prev.rssi:
                        self._pending[current_mac] = BleObs(
                            mac=current_mac,
                            name=names.get(current_mac, ""),
                            rssi=rssi,
                            first_seen=time.time(),
                        )
                    elif not prev.name:
                        names_now = names.get(current_mac, "")
                        if names_now:
                            self._pending[current_mac] = BleObs(
                                mac=prev.mac, name=names_now,
                                rssi=prev.rssi, first_seen=prev.first_seen,
                            )
        # Hand over whatever the last partial window collected.
        self._flush()


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


def parse_bluetoothctl_lines(lines: list[str], now: float) -> list[BleObs]:
    """Pure-python parser for unit tests — fed a list of bluetoothctl output lines."""
    cache: dict[str, BleObs] = {}
    names: dict[str, str] = {}
    current_mac: str | None = None
    for raw in lines:
        line = _strip_ansi(raw).strip()
        d = _DEVICE_RE.search(line)
        if d:
            current_mac = d.group(1).lower()
            trailing = (d.group(2) or "").strip()
            if trailing and not trailing.lower().startswith(("rssi", "txpower", "uuid", "manufacturer")):
                names.setdefault(current_mac, trailing)
        n = _NAME_RE.search(line)
        if n and current_mac:
            names[current_mac] = n.group(1).strip()
        r = _RSSI_RE.search(line)
        if r and current_mac:
            cache[current_mac] = BleObs(
                mac=current_mac,
                name=names.get(current_mac, ""),
                rssi=int(r.group(1) or r.group(2)),
                first_seen=now,
            )
    return list(cache.values())
