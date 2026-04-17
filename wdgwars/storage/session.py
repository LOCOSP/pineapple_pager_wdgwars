"""WigleWifi-1.6 CSV session writer with size-based rotation."""

from __future__ import annotations

import datetime as _dt
import os
import time
from dataclasses import dataclass
from pathlib import Path

from scanners.gps import GpsSnapshot
from .dedup import TtlDedup


WIGLE_HEADER = (
    "WigleWifi-1.6,appRelease=1.0.0,model=Hak5 Pager,release=1.0.0,"
    "device=hak5pager,display=lcd320,board=pineapple-pager,brand=Hak5 Pager"
)
COLUMNS = (
    "MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,"
    "CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type"
)


@dataclass
class SessionStats:
    rows_written: int = 0
    wifi_total: int = 0
    ble_total: int = 0
    files: list[str] = None

    def __post_init__(self) -> None:
        if self.files is None:
            self.files = []


class Session:
    def __init__(self, root_dir: Path, max_file_mb: int = 30,
                 dedup_ttl_s: float = 60.0) -> None:
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_file_mb * 1024 * 1024
        self.dedup = TtlDedup(dedup_ttl_s)
        self.stats = SessionStats()
        self._fh = None
        self._cur_path: Path | None = None
        self.session_id = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        self._open_file()

    def _open_file(self) -> None:
        idx = len(self.stats.files)
        path = self.root / f"wd-{self.session_id}-{idx:02d}.csv"
        self._fh = path.open("w", encoding="utf-8")
        self._fh.write(WIGLE_HEADER + "\n")
        self._fh.write(COLUMNS + "\n")
        self._fh.flush()
        self._cur_path = path
        self.stats.files.append(str(path))

    def _maybe_rotate(self) -> None:
        if self._cur_path and self._cur_path.stat().st_size >= self.max_bytes:
            self.close()
            self._open_file()

    def add_wifi(self, obs, gps: GpsSnapshot) -> bool:
        if not self.dedup.should_write("wifi", obs.bssid, obs.first_seen):
            return False
        ts = _fmt_ts(obs.first_seen)
        row = ",".join([
            obs.bssid,
            _csv_escape(obs.ssid),
            obs.auth,
            ts,
            str(obs.channel),
            str(obs.frequency),
            str(obs.rssi),
            f"{gps.lat:.7f}",
            f"{gps.lon:.7f}",
            f"{gps.alt_m:.1f}",
            f"{gps.accuracy_m:.1f}",
            "",
            "0",
            "WIFI",
        ])
        self._fh.write(row + "\n")
        self._fh.flush()
        self.stats.rows_written += 1
        self.stats.wifi_total += 1
        self._maybe_rotate()
        return True

    def add_ble(self, obs, gps: GpsSnapshot) -> bool:
        if not self.dedup.should_write("ble", obs.mac, obs.first_seen):
            return False
        ts = _fmt_ts(obs.first_seen)
        row = ",".join([
            obs.mac,
            _csv_escape(obs.name),
            "[BLE]",
            ts,
            "0",
            "0",
            str(obs.rssi),
            f"{gps.lat:.7f}",
            f"{gps.lon:.7f}",
            f"{gps.alt_m:.1f}",
            f"{gps.accuracy_m:.1f}",
            "",
            "0",
            "BLE",
        ])
        self._fh.write(row + "\n")
        self._fh.flush()
        self.stats.rows_written += 1
        self.stats.ble_total += 1
        self._maybe_rotate()
        return True

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def _fmt_ts(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _csv_escape(value: str) -> str:
    """Escape commas/quotes/newlines per CSV. Wigle accepts double-quote-wrapped fields."""
    if value is None:
        return ""
    needs_quote = any(c in value for c in (",", '"', "\n", "\r"))
    cleaned = value.replace("\r", " ").replace("\n", " ")
    if needs_quote:
        return '"' + cleaned.replace('"', '""') + '"'
    return cleaned


def list_pending(root: Path) -> list[Path]:
    """Return CSVs that have not been marked .uploaded, sorted oldest-first."""
    root = Path(root)
    if not root.exists():
        return []
    out: list[Path] = []
    for f in root.glob("wd-*.csv"):
        if (f.with_suffix(f.suffix + ".uploaded")).exists():
            continue
        out.append(f)
    out.sort(key=lambda p: p.stat().st_mtime)
    return out


def list_all(root: Path) -> list[tuple[Path, str]]:
    """Return (path, status) tuples — status is 'ok' / 'pending' / 'error'."""
    root = Path(root)
    if not root.exists():
        return []
    out: list[tuple[Path, str]] = []
    for f in sorted(root.glob("wd-*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
        if (f.with_suffix(f.suffix + ".uploaded")).exists():
            out.append((f, "ok"))
        elif (f.with_suffix(f.suffix + ".error")).exists():
            out.append((f, "error"))
        else:
            out.append((f, "pending"))
    return out


def mark_uploaded(path: Path, response_json: str) -> None:
    marker = path.with_suffix(path.suffix + ".uploaded")
    marker.write_text(response_json, encoding="utf-8")


def mark_error(path: Path, message: str) -> None:
    marker = path.with_suffix(path.suffix + ".error")
    marker.write_text(f"{int(time.time())}\n{message}", encoding="utf-8")
