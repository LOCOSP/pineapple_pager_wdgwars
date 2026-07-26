"""WiFi scanner backed by `iw dev <iface> scan`.

Runs the scan on a background thread; new observations are pushed to a queue.
The parser is decoupled so it can be unit-tested on captured fixtures.

Two things make this more than a `subprocess.run` wrapper:

* **Band rotation.** The Pager's primary radio is a tri-band (2.4/5/6 GHz)
  PHY, so a full sweep touches ~90 channels and takes the better part of ten
  seconds — during which a car at 50 km/h covers 140 m. We instead rotate
  through short per-band passes so a position sample lands every ~2 s.

* **Cache-age awareness.** `iw scan` returns the *kernel's BSS cache*, not
  "what this sweep saw" — cfg80211 keeps entries for ~30 s. Left unhandled
  that back-dates nothing and geo-tags 30 s old sightings with the current
  position. We ask the kernel to flush before each sweep, parse the
  `last seen: N ms ago` field, and back-date `first_seen` accordingly.
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

from .wigle_auth import (
    AKM_EAP, AKM_EAP_SUITE_B, AKM_OWE, AKM_PSK, AKM_SAE, build_auth,
)


@dataclass
class WifiObs:
    bssid: str
    ssid: str
    channel: int
    frequency: int
    rssi: int
    auth: str        # already in Wigle bracket form, e.g. "[WPA2-PSK-CCMP][ESS]"
    first_seen: float
    age_s: float = 0.0   # how long before the scan completed this was heard


_BSS_RE = re.compile(r"^BSS\s+([0-9a-fA-F:]{17})", re.MULTILINE)
_FREQ_RE = re.compile(r"^\s*freq:\s*(\d+)", re.MULTILINE)
_SIG_RE = re.compile(r"^\s*signal:\s*(-?\d+\.?\d*)\s*dBm", re.MULTILINE)
_SSID_RE = re.compile(r"^\s*SSID:\s*(.*)$", re.MULTILINE)
_CAP_RE = re.compile(r"^\s*capability:\s*(.*)$", re.MULTILINE)
_LASTSEEN_RE = re.compile(r"^\s*last seen:\s*(\d+)\s*ms ago", re.MULTILINE)
_AUTHSUITES_RE = re.compile(r"Authentication suites:\s*(.+)")
_PAIRWISE_RE = re.compile(r"Pairwise ciphers:\s*(.+)")
_GROUP_RE = re.compile(r"Group cipher:\s*(.+)")
_PHY_FREQ_RE = re.compile(r"^\s*\*\s*(\d+)(?:\.\d+)?\s*MHz\s*\[\s*\d+\s*\]", re.MULTILINE)


# ── channel plans ──────────────────────────────────────────────────────────
# 20 MHz centre frequencies. A short pass beats one long sweep: you trade
# per-cycle completeness for spatial resolution, and rotation gets the
# completeness back across a handful of cycles.

BAND_2G = [2412, 2417, 2422, 2427, 2432, 2437, 2442,
           2447, 2452, 2457, 2462, 2467, 2472, 2484]
# UNII-1 + UNII-3: active scan, no radar dwell, ~30 ms/channel.
BAND_5G_FAST = [5180, 5200, 5220, 5240, 5745, 5765, 5785, 5805, 5825]
# UNII-2A/2C: DFS, must be scanned passively (~110 ms/channel).
BAND_5G_DFS = [5260, 5280, 5300, 5320, 5500, 5520, 5540, 5560,
               5580, 5600, 5620, 5640, 5660, 5680, 5700, 5720]
# 6 GHz Preferred Scanning Channels — the 15 the spec says APs advertise on.
# Sweeping all 59 would cost ~7 s for almost no extra hit rate.
BAND_6G_PSC = [5955, 6035, 6115, 6195, 6275, 6355, 6435, 6515,
               6595, 6675, 6755, 6835, 6915, 6995, 7075]

BANDS: dict[str, list[int]] = {
    "2g": BAND_2G,
    "5g_fast": BAND_5G_FAST,
    "5g_dfs": BAND_5G_DFS,
    "6g_psc": BAND_6G_PSC,
    "all": [],          # empty list = let the driver sweep everything
}

# 2.4 GHz holds most of what a wardrive logs, so it gets every other slot.
DEFAULT_PLAN = ["2g", "5g_fast", "2g", "5g_dfs", "2g", "5g_fast", "2g", "6g_psc"]


def parse_iw_scan(text: str, ts: float | None = None) -> list[WifiObs]:
    """Parse the textual output of `iw dev <iface> scan` into observations.

    *ts* is the time the scan completed; each observation is back-dated by the
    BSS's `last seen` age so callers can geo-tag it against the position that
    was true when the frame actually arrived.
    """
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
        ssid = _decode_ssid(ssid_m.group(1).strip()) if ssid_m else ""

        freq_m = _FREQ_RE.search(block)
        freq = int(freq_m.group(1)) if freq_m else 0
        channel = _freq_to_channel(freq)

        sig_m = _SIG_RE.search(block)
        rssi = int(float(sig_m.group(1))) if sig_m else 0

        cap_m = _CAP_RE.search(block)
        cap_line = cap_m.group(1) if cap_m else ""

        seen_m = _LASTSEEN_RE.search(block)
        age_s = (int(seen_m.group(1)) / 1000.0) if seen_m else 0.0

        auth = _classify_auth(block, cap_line)

        out.append(WifiObs(
            bssid=bssid, ssid=ssid, channel=channel, frequency=freq,
            rssi=rssi, auth=auth, first_seen=ts - age_s, age_s=age_s,
        ))
    return out


def _decode_ssid(raw: str) -> str:
    r"""Undo iw's `\xNN` escaping and drop non-printables.

    iw prints any byte outside printable ASCII as `\xNN`, so a UTF-8 SSID
    arrives as a literal backslash-x soup. Without this, "Dom Kowalskich"
    with a Polish diacritic lands in the CSV as `Dom Kowalskich\xc5\x82`.
    Hidden SSIDs (all `\x00`) collapse to "" as before.
    """
    # Trigger on any backslash, not just `\x`: iw also escapes a literal
    # backslash in an SSID as `\\`, and that needs collapsing too.
    if "\\" in raw:
        buf = bytearray()
        i = 0
        while i < len(raw):
            if raw.startswith("\\x", i) and i + 4 <= len(raw):
                try:
                    buf.append(int(raw[i + 2:i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
            if raw.startswith("\\\\", i):
                buf.append(0x5C)
                i += 2
                continue
            buf.extend(raw[i].encode("utf-8", errors="replace"))
            i += 1
        raw = buf.decode("utf-8", errors="replace")
    return "".join(ch for ch in raw if ch.isprintable() and ch != "\x00")


def _freq_to_channel(freq: int) -> int:
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq == 5935:            # 6 GHz channel 2 sits below the linear range
        return 2
    if 5955 <= freq <= 7115:    # 6 GHz
        return (freq - 5950) // 5
    if 5000 <= freq < 5900:     # 5 GHz, incl. ch 32 (5160) and ch 173 (5865)
        return (freq - 5000) // 5
    if 4910 <= freq < 5000:     # 4.9 GHz public safety / Japan
        return (freq - 4000) // 5
    return 0


# ── auth classification ────────────────────────────────────────────────────

def _subsection(block: str, name: str) -> str:
    """Return the indented body of a `name:` sub-block (e.g. RSN, WPA, WPS).

    Indentation-relative rather than tab-count-based, so it survives the
    formatting differences between iw releases.
    """
    m = re.search(rf"^([ \t]*){re.escape(name)}:(.*)$", block, re.MULTILINE)
    if not m:
        return ""
    base = len(m.group(1).expandtabs(8))
    out = [m.group(2)]
    for line in block[m.end():].split("\n")[1:]:
        if not line.strip():
            continue
        indent = len(line[:len(line) - len(line.lstrip())].expandtabs(8))
        if indent <= base:
            break
        out.append(line)
    return "\n".join(out)


_KNOWN_CIPHERS = ("CCMP-256", "GCMP-256", "GCMP", "CCMP", "TKIP",
                  "WEP-104", "WEP-40")


def _ciphers(section: str) -> list[str]:
    """Cipher names mentioned by a security section, e.g. ["CCMP", "TKIP"]."""
    found: list[str] = []
    for rx in (_PAIRWISE_RE, _GROUP_RE):
        m = rx.search(section)
        if not m:
            continue
        for tok in m.group(1).split():
            tok = tok.strip().upper()
            if tok in _KNOWN_CIPHERS and tok not in found:
                found.append(tok)
    if not found:
        # RSN without a parsable pairwise list is CCMP by definition.
        found = [c for c in ("CCMP", "TKIP") if c in section]
    return found


def _akms(section: str) -> set[str]:
    """AKM tokens advertised by a security section."""
    m = _AUTHSUITES_RE.search(section)
    suites = (m.group(1) if m else "").upper()
    akms: set[str] = set()
    if "SAE" in suites:
        akms.add(AKM_SAE)
    if "OWE" in suites:
        akms.add(AKM_OWE)
    if "PSK" in suites:
        akms.add(AKM_PSK)
    if "SUITE-B" in suites:
        akms.add(AKM_EAP_SUITE_B)
    elif "802.1X" in suites or "EAP" in suites:
        akms.add(AKM_EAP)
    return akms


def _sec(section: str) -> dict | None:
    if not section:
        return None
    return {"ciphers": _ciphers(section), "akms": _akms(section)}


_CAP_HEX_RE = re.compile(r"\((0x[0-9a-fA-F]+)\)")

CAP_ESS = 0x0001
CAP_IBSS = 0x0002
CAP_PRIVACY = 0x0010


def _capability_flags(capability: str) -> tuple[bool, bool]:
    """``(privacy, ess)`` from a capability line.

    The hex value is authoritative. iw 6.9 on this hardware prints only
    `capability: ESS (0x0431)` — no `Privacy` word — even though bit 4 is set,
    so text matching alone silently classifies every WEP network as open.
    Older iw releases print the words, hence the fallback.
    """
    m = _CAP_HEX_RE.search(capability)
    if m:
        bits = int(m.group(1), 16)
        return bool(bits & CAP_PRIVACY), not bool(bits & CAP_IBSS)
    words = capability.split()
    return "Privacy" in words, "IBSS" not in words


def _classify_auth(block: str, capability: str) -> str:
    """Build the Wigle `AuthMode` bracket string for a BSS block."""
    privacy, ess = _capability_flags(capability)
    return build_auth(
        privacy=privacy,
        ess=ess,
        wpa=_sec(_subsection(block, "WPA")),
        rsn=_sec(_subsection(block, "RSN")),
        wps=bool(_subsection(block, "WPS")) or "WPS:" in block,
    )


# ── scanner ────────────────────────────────────────────────────────────────

class WifiScanner:
    """Background WiFi scanner; call `start()` then `drain()` periodically."""

    def __init__(self, iface: str = "wlan0", interval_s: float = 0.0,
                 plan: list[str] | None = None, flush_cache: bool = True,
                 max_age_s: float | None = None,
                 full_sweep_timeout_s: float = 30.0,
                 queue_max: int = 64) -> None:
        self.iface = iface
        self.interval = interval_s
        self.plan = [b for b in (plan or DEFAULT_PLAN) if b in BANDS] or ["all"]
        self.flush_cache = flush_cache
        self.max_age_s = max_age_s
        self.full_sweep_timeout_s = full_sweep_timeout_s

        # Bounded: a modal dialog can stall the consumer for as long as the
        # user leaves it open, and an unbounded queue would just grow.
        self._q: queue.Queue[list[WifiObs]] = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._thr: threading.Thread | None = None
        self._plan_idx = 0
        self._supported: set[int] | None = None
        self._use_flush = flush_cache
        # Consecutive failures per band. If `iw phy` probing came back empty
        # we may be asking the radio for channels it does not have; retire a
        # band rather than burning a rotation slot on it every cycle.
        self._band_fail: dict[str, int] = {}

        self.available: bool = False
        self.last_error: str | None = None
        self.scan_count: int = 0
        self.timeout_count: int = 0
        self.dropped_stale: int = 0
        self.dropped_batches: int = 0
        self.last_scan_s: float = 0.0
        self.last_band: str = "-"

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thr and self._thr.is_alive():
            return
        if not shutil.which("iw"):
            self.last_error = "`iw` not installed (opkg install iw)"
            self.available = False
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="wifi-scan", daemon=True)
        self._thr.start()
        self.available = True

    def stop(self) -> None:
        self._stop.set()
        if self._thr:
            self._thr.join(timeout=3)
            self._thr = None
        self.available = False

    def drain(self) -> list[WifiObs]:
        out: list[WifiObs] = []
        while True:
            try:
                out.extend(self._q.get_nowait())
            except queue.Empty:
                return out

    # ── band plan ──────────────────────────────────────────────────────────

    def _wiphy_index(self) -> int | None:
        """Which wiphy backs this interface, per `iw dev <iface> info`."""
        try:
            proc = subprocess.run(["iw", "dev", self.iface, "info"],
                                  capture_output=True, text=True, timeout=10)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        m = re.search(r"^\s*wiphy\s+(\d+)", proc.stdout, re.MULTILINE)
        return int(m.group(1)) if m else None

    def _probe_supported(self) -> set[int]:
        """Frequencies *this* radio will accept.

        Asking for an unsupported channel makes `iw scan` fail outright, so
        every band plan is intersected with this set once at startup rather
        than discovering it the hard way on every cycle.

        Scoped to the interface's own wiphy on purpose: bare `iw phy` dumps
        every radio in the device, and on the Pager that means a scan on the
        2.4 GHz-only phy0 would happily be handed 5 and 6 GHz channels that
        only phy1 supports.
        """
        idx = self._wiphy_index()
        cmd = ["iw", f"phy#{idx}", "info"] if idx is not None else ["iw", "phy"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=10)
        except Exception:
            return set()
        if proc.returncode != 0 and idx is not None:
            # Fall back to the all-radio dump rather than scanning blind.
            try:
                proc = subprocess.run(["iw", "phy"], capture_output=True,
                                      text=True, timeout=10)
            except Exception:
                return set()
        if proc.returncode != 0:
            return set()
        freqs: set[int] = set()
        for line in proc.stdout.splitlines():
            if "disabled" in line:
                continue
            m = _PHY_FREQ_RE.match(line)
            if m:
                freqs.add(int(m.group(1)))
        return freqs

    BAND_FAIL_LIMIT = 3

    def _next_band(self) -> tuple[str, list[int]]:
        """Advance the rotation, skipping bands this radio cannot scan."""
        for _ in range(len(self.plan)):
            name = self.plan[self._plan_idx % len(self.plan)]
            self._plan_idx += 1
            if self._band_fail.get(name, 0) >= self.BAND_FAIL_LIMIT:
                continue
            freqs = BANDS[name]
            if not freqs:                       # "all" → full sweep
                return name, []
            if self._supported:
                freqs = [f for f in freqs if f in self._supported]
            if freqs:
                return name, freqs
        # Every band retired itself — fall back to letting the driver decide.
        return "all", []

    def _timeout_for(self, freqs: list[int]) -> float:
        if not freqs:
            return self.full_sweep_timeout_s
        # DFS/6 GHz dwell passively at ~110 ms; budget generously and still
        # come in well under the full-sweep timeout.
        return max(10.0, 5.0 + 0.25 * len(freqs))

    # ── background thread ─────────────────────────────────────────────────

    def _run(self) -> None:
        self._supported = self._probe_supported()
        while not self._stop.is_set():
            band, freqs = self._next_band()
            self.last_band = band
            cmd = ["iw", "dev", self.iface, "scan"]
            if self._use_flush:
                cmd.append("flush")
            if freqs:
                cmd.append("freq")
                cmd.extend(str(f) for f in freqs)

            t0 = time.monotonic()
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=self._timeout_for(freqs))
                self.last_scan_s = time.monotonic() - t0
                if proc.returncode == 0:
                    self._ingest(proc.stdout)
                    self.last_error = None
                    self.scan_count += 1
                    self._band_fail.pop(band, None)
                else:
                    err = (proc.stderr or "").strip()
                    # Old iw builds (and some drivers) reject the flush flag.
                    # Drop it once and retry the same band before blaming it.
                    if self._use_flush and _looks_like_usage_error(err):
                        self._use_flush = False
                        self._plan_idx -= 1
                        continue
                    self._band_fail[band] = self._band_fail.get(band, 0) + 1
                    self.last_error = err.splitlines()[0] if err else f"rc={proc.returncode}"
            except subprocess.TimeoutExpired:
                self.last_scan_s = time.monotonic() - t0
                self.timeout_count += 1
                self.last_error = f"scan timeout ({band})"
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
            if self.interval:
                self._stop.wait(self.interval)

    def _ingest(self, stdout: str) -> None:
        now = time.time()
        obs = parse_iw_scan(stdout, ts=now)
        if not obs:
            return
        # Anything older than the sweep we just ran came out of the kernel's
        # BSS cache, not off the air — geo-tagging it here would be a lie.
        limit = self.max_age_s
        if limit is None:
            limit = max(4.0, self.last_scan_s + 2.0)
        fresh = [o for o in obs if o.age_s <= limit]
        self.dropped_stale += len(obs) - len(fresh)
        if not fresh:
            return
        try:
            self._q.put_nowait(fresh)
        except queue.Full:
            # Drop the oldest sweep rather than block the scanner thread —
            # newer observations are the ones worth keeping.
            try:
                self._q.get_nowait()
                self._q.put_nowait(fresh)
            except (queue.Empty, queue.Full):
                pass
            self.dropped_batches += 1


def _looks_like_usage_error(stderr: str) -> bool:
    low = stderr.lower()
    return ("usage" in low or "invalid argument" in low
            or "not supported" in low or "command failed" in low)
