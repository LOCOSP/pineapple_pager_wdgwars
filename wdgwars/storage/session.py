"""WigleWifi-1.6 CSV session writer with size-based rotation.

The writer used to `flush()` and `stat()` on every single row. That is a
syscall pair per observation on eMMC for no benefit — the data is still lost
on a hard power cut either way, because nothing was ever `fsync`ed. Here the
file is block-buffered, size is tracked in a counter, flushes are time-based
(so `tail -f`/`wc -l` over SSH still shows near-live progress), and a periodic
`fsync` gives the durability the old code only appeared to have.
"""

from __future__ import annotations

import datetime as _dt
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from scanners.gps import GpsSnapshot
from .dedup import GeoDedup


WIGLE_HEADER = (
    "WigleWifi-1.6,appRelease=1.0.0,model=Hak5 Pager,release=1.0.0,"
    "device=hak5pager,display=lcd320,board=pineapple-pager,brand=Hak5 Pager"
)
COLUMNS = (
    "MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,"
    "CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type"
)

# WiGLE downweights rows claiming a zero-metre error, so never emit one.
MIN_ACCURACY_M = 1.0


@dataclass
class SessionStats:
    rows_written: int = 0
    wifi_total: int = 0
    ble_total: int = 0
    skipped_dedup: int = 0
    skipped_no_fix: int = 0
    files: list[str] = field(default_factory=list)


class Session:
    def __init__(self, root_dir: Path, max_file_mb: int = 30,
                 refresh_ttl_s: float = 300.0, min_move_m: float = 30.0,
                 rssi_delta_db: int = 6, require_fix: bool = True,
                 flush_interval_s: float = 2.0,
                 fsync_interval_s: float = 20.0) -> None:
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_file_mb * 1024 * 1024
        self.dedup = GeoDedup(ttl_s=refresh_ttl_s, min_move_m=min_move_m,
                              rssi_delta_db=rssi_delta_db)
        self.require_fix = require_fix
        self.flush_interval_s = flush_interval_s
        self.fsync_interval_s = fsync_interval_s
        self.stats = SessionStats()

        self._fh = None
        self._cur_path: Path | None = None
        self._bytes = 0
        self._last_flush = time.monotonic()
        self._last_fsync = self._last_flush
        self.session_id = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        self._open_file()

    # ── file handling ──────────────────────────────────────────────────────

    def _open_file(self) -> None:
        idx = len(self.stats.files)
        path = self.root / f"wd-{self.session_id}-{idx:02d}.csv"
        self._fh = path.open("w", encoding="utf-8", buffering=1 << 16)
        header = WIGLE_HEADER + "\n" + COLUMNS + "\n"
        self._fh.write(header)
        self._fh.flush()
        self._bytes = len(header.encode("utf-8"))
        self._last_flush = time.monotonic()
        self._cur_path = path
        self.stats.files.append(str(path))

    def _maybe_rotate(self) -> None:
        if self._bytes >= self.max_bytes:
            self.close()
            self._open_file()

    def _maybe_flush(self) -> None:
        """Time-based flush, with a slower fsync behind it.

        Flushing every row cost a write(2) per observation; flushing on a
        timer keeps an SSH `wc -l` essentially live while making the syscall
        rate independent of how dense the RF environment is.
        """
        now = time.monotonic()
        if now - self._last_flush < self.flush_interval_s:
            return
        try:
            self._fh.flush()
            self._last_flush = now
            if now - self._last_fsync >= self.fsync_interval_s:
                os.fsync(self._fh.fileno())
                self._last_fsync = now
        except Exception:
            pass

    def flush(self) -> None:
        """Force everything buffered out to the filesystem right now."""
        if not self._fh:
            return
        try:
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except Exception:
            pass
        self._last_flush = self._last_fsync = time.monotonic()

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except Exception:
                pass
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    # ── row writers ────────────────────────────────────────────────────────

    def _write_row(self, fields: list[str]) -> None:
        row = ",".join(fields) + "\n"
        self._fh.write(row)
        self._bytes += len(row.encode("utf-8"))
        self.stats.rows_written += 1
        self._maybe_flush()
        self._maybe_rotate()

    def _gate(self, kind: str, mac: str, obs_time: float,
              gps: GpsSnapshot, rssi: int) -> bool:
        """Common accept/reject for a sighting.

        Refusing to write without a fix is the whole point: `GpsState` keeps
        the last known lat/lon for the HUD after a dropout, and writing those
        would pin every AP in a tunnel to the coordinate where the fix died.
        """
        if self.require_fix and not gps.fix_3d:
            self.stats.skipped_no_fix += 1
            return False
        if not self.dedup.should_write(kind, mac, obs_time, gps.lat, gps.lon, rssi):
            self.stats.skipped_dedup += 1
            return False
        return True

    def add_wifi(self, obs, gps: GpsSnapshot) -> bool:
        if not self._gate("wifi", obs.bssid, obs.first_seen, gps, obs.rssi):
            return False
        self._write_row([
            obs.bssid,
            _csv_escape(obs.ssid),
            obs.auth,
            _fmt_ts(obs.first_seen),
            str(obs.channel),
            str(obs.frequency),
            str(obs.rssi),
            f"{gps.lat:.7f}",
            f"{gps.lon:.7f}",
            f"{gps.alt_m:.1f}",
            f"{max(gps.accuracy_m, MIN_ACCURACY_M):.1f}",
            "",
            "0",
            "WIFI",
        ])
        self.stats.wifi_total += 1
        return True

    def add_ble(self, obs, gps: GpsSnapshot) -> bool:
        if not self._gate("ble", obs.mac, obs.first_seen, gps, obs.rssi):
            return False
        self._write_row([
            obs.mac,
            _csv_escape(obs.name),
            "[BLE]",
            _fmt_ts(obs.first_seen),
            "0",
            "0",
            str(obs.rssi),
            f"{gps.lat:.7f}",
            f"{gps.lon:.7f}",
            f"{gps.alt_m:.1f}",
            f"{max(gps.accuracy_m, MIN_ACCURACY_M):.1f}",
            "",
            "0",
            "BLE",
        ])
        self.stats.ble_total += 1
        return True


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


def _scan_sessions(root: Path) -> list[tuple[Path, float, set[str]]]:
    """One directory walk producing (csv, mtime, marker suffixes present).

    The old listing helpers called `Path.stat()` inside a sort key and probed
    for marker files with `exists()` per candidate — three syscalls per file
    per menu build. `scandir` already carries the metadata.
    """
    root = Path(root)
    if not root.exists():
        return []
    csvs: dict[str, tuple[Path, float]] = {}
    markers: dict[str, set[str]] = {}
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for e in entries:
        if not e.is_file():
            continue
        name = e.name
        if name.startswith("wd-") and name.endswith(".csv"):
            try:
                csvs[name] = (Path(e.path), e.stat().st_mtime)
            except OSError:
                continue
        elif name.endswith(".uploaded") or name.endswith(".error"):
            base, _, suffix = name.rpartition(".")
            markers.setdefault(base, set()).add(suffix)
    return [(p, mt, markers.get(name, set()))
            for name, (p, mt) in csvs.items()]


def list_pending(root: Path) -> list[Path]:
    """Return CSVs that have not been marked .uploaded, sorted oldest-first."""
    rows = [(p, mt) for p, mt, marks in _scan_sessions(root)
            if "uploaded" not in marks]
    rows.sort(key=lambda r: r[1])
    return [p for p, _ in rows]


def list_all(root: Path) -> list[tuple[Path, str]]:
    """Return (path, status) tuples — status is 'ok' / 'pending' / 'error'."""
    rows = _scan_sessions(root)
    rows.sort(key=lambda r: r[1], reverse=True)
    out: list[tuple[Path, str]] = []
    for path, _mt, marks in rows:
        if "uploaded" in marks:
            out.append((path, "ok"))
        elif "error" in marks:
            out.append((path, "error"))
        else:
            out.append((path, "pending"))
    return out


def mark_uploaded(path: Path, response_json: str) -> None:
    marker = path.with_suffix(path.suffix + ".uploaded")
    marker.write_text(response_json, encoding="utf-8")


def mark_error(path: Path, message: str) -> None:
    marker = path.with_suffix(path.suffix + ".error")
    marker.write_text(f"{int(time.time())}\n{message}", encoding="utf-8")
