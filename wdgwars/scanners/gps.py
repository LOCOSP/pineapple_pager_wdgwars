"""GPS reader for u-blox 7 USB stick (CDC-ACM, NMEA at 9600 8N1).

Runs a background thread that opens the first available `/dev/ttyACM*` device
and parses NMEA `$GPGGA` and `$GPRMC` sentences into a shared GpsState.
"""

from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

try:
    import termios
    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False


@dataclass
class GpsState:
    fix_3d: bool = False
    fix_quality: int = 0
    lat: float = 0.0
    lon: float = 0.0
    alt_m: float = 0.0
    accuracy_m: float = 0.0
    sats: int = 0
    utc_iso: str = ""
    last_update: float = 0.0
    device: str = ""

    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def snapshot(self) -> "GpsSnapshot":
        with self.lock:
            return GpsSnapshot(
                fix_3d=self.fix_3d, fix_quality=self.fix_quality,
                lat=self.lat, lon=self.lon, alt_m=self.alt_m,
                accuracy_m=self.accuracy_m, sats=self.sats,
                utc_iso=self.utc_iso, last_update=self.last_update,
                device=self.device,
            )


@dataclass(frozen=True)
class GpsSnapshot:
    fix_3d: bool
    fix_quality: int
    lat: float
    lon: float
    alt_m: float
    accuracy_m: float
    sats: int
    utc_iso: str
    last_update: float
    device: str


def parse_nmea(line: str) -> dict | None:
    """Parse a single NMEA line. Returns dict with normalised fields or None."""
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return None
    body, _, csum = line[1:].partition("*")
    if not _checksum_ok(body, csum):
        return None
    parts = body.split(",")
    talker_sentence = parts[0]
    sentence = talker_sentence[2:] if len(talker_sentence) >= 5 else talker_sentence

    if sentence == "GGA" and len(parts) >= 10:
        return _parse_gga(parts)
    if sentence == "RMC" and len(parts) >= 10:
        return _parse_rmc(parts)
    return None


def _parse_gga(p: list[str]) -> dict | None:
    if not p[2] or not p[4]:
        return {"sentence": "GGA", "fix_quality": _to_int(p[6]), "sats": _to_int(p[7])}
    return {
        "sentence": "GGA",
        "utc": p[1],
        "lat": _nmea_to_deg(p[2], p[3]),
        "lon": _nmea_to_deg(p[4], p[5]),
        "fix_quality": _to_int(p[6]),
        "sats": _to_int(p[7]),
        "hdop": _to_float(p[8]),
        "alt_m": _to_float(p[9]),
    }


def _parse_rmc(p: list[str]) -> dict | None:
    status = p[2]
    if status != "A":
        return {"sentence": "RMC", "active": False}
    return {
        "sentence": "RMC",
        "active": True,
        "utc": p[1],
        "lat": _nmea_to_deg(p[3], p[4]),
        "lon": _nmea_to_deg(p[5], p[6]),
        "speed_kn": _to_float(p[7]),
        "date": p[9],
    }


def _nmea_to_deg(value: str, hemi: str) -> float:
    if not value:
        return 0.0
    try:
        if "." not in value:
            return 0.0
        dot = value.index(".")
        deg = int(value[: dot - 2])
        minutes = float(value[dot - 2:])
        v = deg + minutes / 60.0
        if hemi in ("S", "W"):
            v = -v
        return v
    except (ValueError, IndexError):
        return 0.0


def _to_int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return 0


def _to_float(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return 0.0


def _checksum_ok(body: str, csum: str) -> bool:
    try:
        target = int(csum.strip()[:2], 16)
    except ValueError:
        return False
    acc = 0
    for ch in body:
        acc ^= ord(ch)
    return acc == target


def _utc_to_iso(date_ddmmyy: str, utc_hhmmss: str) -> str:
    if len(date_ddmmyy) != 6 or len(utc_hhmmss) < 6:
        return ""
    dd, mm, yy = date_ddmmyy[:2], date_ddmmyy[2:4], date_ddmmyy[4:6]
    hh, mi, ss = utc_hhmmss[:2], utc_hhmmss[2:4], utc_hhmmss[4:6]
    return f"20{yy}-{mm}-{dd} {hh}:{mi}:{ss}"


class GpsReader:
    def __init__(self, devices: Iterable[str], baud: int = 9600,
                 min_sats: int = 4) -> None:
        self.devices = list(devices)
        self.baud = baud
        self.min_sats = min_sats
        self.state = GpsState()
        self._stop = threading.Event()
        self._thr: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="gps", daemon=True)
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thr:
            self._thr.join(timeout=2)
            self._thr = None

    def _open(self) -> tuple[int, str] | None:
        """Try each candidate device; pick the first one emitting NMEA within ~3s."""
        for dev in self.devices:
            if not os.path.exists(dev):
                continue
            try:
                fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
            except OSError:
                continue
            if HAS_TERMIOS:
                try:
                    self._configure_serial(fd)
                except Exception:
                    pass
            if self._validate_nmea(fd):
                return fd, dev
            os.close(fd)
        return None

    def _validate_nmea(self, fd: int, timeout_s: float = 3.0) -> bool:
        """Return True if the device emits at least one $G... line within timeout."""
        deadline = time.time() + timeout_s
        buf = b""
        while time.time() < deadline and not self._stop.is_set():
            try:
                chunk = os.read(fd, 256)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            except OSError:
                return False
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk
            for line in buf.split(b"\n"):
                if line.startswith(b"$G") and b"*" in line:
                    return True
            buf = buf[-256:]
        return False

    def _configure_serial(self, fd: int) -> None:
        attrs = termios.tcgetattr(fd)
        speed = getattr(termios, f"B{self.baud}", termios.B9600)
        attrs[4] = speed
        attrs[5] = speed
        # 8N1 raw
        attrs[2] = (attrs[2] & ~termios.CSIZE) | termios.CS8
        attrs[2] &= ~termios.PARENB
        attrs[2] &= ~termios.CSTOPB
        attrs[2] |= termios.CREAD | termios.CLOCAL
        attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHOE | termios.ISIG)
        attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY)
        attrs[1] &= ~termios.OPOST
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def _run(self) -> None:
        buf = b""
        fd: int | None = None
        dev = ""
        last_rmc_date = ""
        while not self._stop.is_set():
            if fd is None:
                opened = self._open()
                if not opened:
                    self._stop.wait(2.0)
                    continue
                fd, dev = opened
                with self.state.lock:
                    self.state.device = dev

            try:
                chunk = os.read(fd, 256)
            except BlockingIOError:
                self._stop.wait(0.1)
                continue
            except OSError:
                os.close(fd)
                fd = None
                continue

            if not chunk:
                self._stop.wait(0.1)
                continue

            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                try:
                    text = line.decode("ascii", errors="ignore").strip("\r\x00 ")
                except Exception:
                    continue
                parsed = parse_nmea(text)
                if not parsed:
                    continue
                self._apply(parsed, last_rmc_date)
                if parsed.get("sentence") == "RMC" and parsed.get("date"):
                    last_rmc_date = parsed["date"]

        if fd is not None:
            os.close(fd)

    def _apply(self, parsed: dict, last_rmc_date: str) -> None:
        with self.state.lock:
            sentence = parsed["sentence"]
            now = time.time()
            self.state.last_update = now
            if sentence == "GGA":
                fq = parsed.get("fix_quality", 0)
                self.state.fix_quality = fq
                self.state.sats = parsed.get("sats", self.state.sats)
                if fq > 0 and "lat" in parsed:
                    self.state.lat = parsed["lat"]
                    self.state.lon = parsed["lon"]
                    self.state.alt_m = parsed.get("alt_m", 0.0)
                    hdop = parsed.get("hdop", 0.0)
                    self.state.accuracy_m = max(2.5, hdop * 5.0) if hdop else 0.0
                self.state.fix_3d = (fq > 0 and self.state.sats >= self.min_sats)
            elif sentence == "RMC":
                if parsed.get("active"):
                    self.state.lat = parsed["lat"]
                    self.state.lon = parsed["lon"]
                    self.state.utc_iso = _utc_to_iso(parsed["date"], parsed["utc"])
                    if not self.state.fix_3d and self.state.sats >= self.min_sats:
                        self.state.fix_3d = True


# silence unused-import warning if struct gets pruned later
_ = struct
