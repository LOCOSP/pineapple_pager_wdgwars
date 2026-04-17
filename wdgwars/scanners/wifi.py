"""WiFi scanner backed by `iw dev <iface> scan`.

Runs the scan on a background thread; new observations are pushed to a queue.
The parser is decoupled so it can be unit-tested on captured fixtures.
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass
class WifiObs:
    bssid: str
    ssid: str
    channel: int
    frequency: int
    rssi: int
    auth: str        # already in Wigle bracket form, e.g. "[WPA2-PSK-CCMP][ESS]"
    first_seen: float


_BSS_RE = re.compile(r"^BSS\s+([0-9a-fA-F:]{17})", re.MULTILINE)
_FREQ_RE = re.compile(r"^\s*freq:\s*(\d+)", re.MULTILINE)
_SIG_RE = re.compile(r"^\s*signal:\s*(-?\d+\.?\d*)\s*dBm", re.MULTILINE)
_SSID_RE = re.compile(r"^\s*SSID:\s*(.*)$", re.MULTILINE)
_CAP_RE = re.compile(r"^\s*capability:\s*(.*)$", re.MULTILINE)


def parse_iw_scan(text: str, ts: float | None = None) -> list[WifiObs]:
    """Parse the textual output of `iw dev <iface> scan` into observations."""
    if ts is None:
        ts = time.time()

    # Split blocks: each starts with "BSS xx:xx:..." line
    matches = list(_BSS_RE.finditer(text))
    out: list[WifiObs] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end]
        bssid = m.group(1).lower()

        ssid_m = _SSID_RE.search(block)
        ssid = ssid_m.group(1).strip() if ssid_m else ""
        # Hidden SSIDs come back empty or as `\x00`s; normalise to ""
        ssid = "".join(ch for ch in ssid if ch.isprintable() and ch != "\x00")

        freq_m = _FREQ_RE.search(block)
        freq = int(freq_m.group(1)) if freq_m else 0
        channel = _freq_to_channel(freq)

        sig_m = _SIG_RE.search(block)
        rssi = int(float(sig_m.group(1))) if sig_m else 0

        cap_m = _CAP_RE.search(block)
        cap_line = cap_m.group(1) if cap_m else ""

        auth = _classify_auth(block, cap_line)

        out.append(WifiObs(
            bssid=bssid, ssid=ssid, channel=channel, frequency=freq,
            rssi=rssi, auth=auth, first_seen=ts,
        ))
    return out


def _freq_to_channel(freq: int) -> int:
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if 5170 <= freq <= 5825:
        return (freq - 5000) // 5
    if 5955 <= freq <= 7115:  # 6 GHz
        return (freq - 5950) // 5
    return 0


def _classify_auth(block: str, capability: str) -> str:
    has_rsn = "RSN:" in block
    has_wpa = re.search(r"^\s*WPA:\s*", block, re.MULTILINE) is not None
    has_sae = "Authentication suites: SAE" in block or "AKM Suites: SAE" in block
    privacy = "Privacy" in capability
    ess = "ESS" in capability or "ESS" not in capability  # default to ESS in CSV

    suffix = "[ESS]"
    parts: list[str] = []

    if has_rsn or has_wpa:
        ccmp = "CCMP" in block
        tkip = "TKIP" in block
        ciphers = "+".join([c for c in ("CCMP", "TKIP") if (c == "CCMP" and ccmp) or (c == "TKIP" and tkip)]) or "CCMP"
        if has_sae:
            parts.append(f"[WPA3-SAE-{ciphers}]")
        if has_rsn and not has_sae:
            parts.append(f"[WPA2-PSK-{ciphers}]")
        if has_wpa:
            parts.append(f"[WPA-PSK-{ciphers}]")
    elif privacy:
        parts.append("[WEP]")

    parts.append(suffix)
    return "".join(parts)


class WifiScanner:
    """Background WiFi scanner; call `start()` then `drain()` periodically."""

    def __init__(self, iface: str = "wlan0", interval_s: float = 8.0) -> None:
        self.iface = iface
        self.interval = interval_s
        self._q: queue.Queue[WifiObs] = queue.Queue()
        self._stop = threading.Event()
        self._thr: threading.Thread | None = None
        self.last_error: str | None = None
        self.scan_count: int = 0

    def start(self) -> None:
        if self._thr and self._thr.is_alive():
            return
        if not shutil.which("iw"):
            self.last_error = "`iw` not installed (opkg install iw)"
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="wifi-scan", daemon=True)
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thr:
            self._thr.join(timeout=2)
            self._thr = None

    def drain(self) -> list[WifiObs]:
        out: list[WifiObs] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                proc = subprocess.run(
                    ["iw", "dev", self.iface, "scan"],
                    capture_output=True, text=True, timeout=12,
                )
                if proc.returncode == 0:
                    for obs in parse_iw_scan(proc.stdout):
                        self._q.put(obs)
                    self.last_error = None
                else:
                    self.last_error = proc.stderr.strip().splitlines()[0] if proc.stderr else f"rc={proc.returncode}"
                self.scan_count += 1
            except subprocess.TimeoutExpired:
                self.last_error = "scan timeout"
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
            self._stop.wait(self.interval)
