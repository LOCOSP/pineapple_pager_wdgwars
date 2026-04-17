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

    def __init__(self, hci: str = "hci0", interval_s: float = 12.0) -> None:
        self.hci = hci
        self.interval = interval_s
        self._q: queue.Queue[BleObs] = queue.Queue()
        self._stop = threading.Event()
        self._thr: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self.last_error: str | None = None
        self.available: bool = False

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
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

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
            for cmd in (b"power on\n", b"agent off\n",
                        b"menu scan\n", b"transport le\n", b"back\n",
                        b"scan on\n"):
                os.write(master_fd, cmd)
                time.sleep(0.05)
        except Exception as e:
            self.last_error = f"setup: {e}"
            return

        current_mac: str | None = None
        names: dict[str, str] = {}
        buf = b""
        while not self._stop.is_set() and self._proc.poll() is None:
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.5)
            except (ValueError, OSError):
                break
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
                    # Push straight into the drain queue so the HUD reacts in
                    # real time. Session-level TTL dedup (default 60 s) still
                    # prevents a spammer from writing the same MAC 100× to CSV;
                    # this just stops the scanner from holding events for up
                    # to `interval_s` seconds before handing them off.
                    self._q.put(BleObs(
                        mac=current_mac,
                        name=names.get(current_mac, ""),
                        rssi=rssi,
                        first_seen=time.time(),
                    ))


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
