"""Passive monitor-mode WiFi capture — the high-yield wardriving backend.

`iw scan` hands you a snapshot every few seconds and nothing in between. A
monitor interface hears *every* beacon: APs beacon ~10×/s, so a 250 ms dwell
on a channel catches essentially everything transmitting on it. On a drive
that is the difference between one position sample per AP per 15 s and one
every time you pass within range.

Requires a monitor-mode interface — either the Pager's own second radio
(`wlan1mon`) or an external adapter such as the Alfa AWUS036ACM brought up as
`wlan2mon` (issue #3). Capture runs through `tcpdump -w -` and the pcap is
decoded here: radiotap for frequency/RSSI, then 802.11 management frames for
BSSID/SSID/security IEs.

Two things keep this affordable on a 580 MHz MIPS core:

* **IEs are parsed once per BSSID.** An AP's SSID and security do not change
  between beacons, so after the first sighting each subsequent frame only
  costs a header parse and a dict lookup.
* **Observations are aggregated into emit windows.** One object per AP per
  second reaches the queue instead of one per beacon, which is ~10× less
  allocation churn for identical information.
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import struct
import subprocess
import threading
import time

from .wifi import WifiObs, _freq_to_channel
from .wigle_auth import (
    AKM_EAP, AKM_EAP_SUITE_B, AKM_OWE, AKM_PSK, AKM_SAE, build_auth,
)

# ── radiotap ───────────────────────────────────────────────────────────────
# (size, alignment) per present-bitmap bit, in the order the spec lays them
# out. We only need bits 0..14; anything beyond that we never walk into.
_RT_FIELDS = (
    (8, 8),   # 0  TSFT
    (1, 1),   # 1  Flags
    (1, 1),   # 2  Rate
    (4, 2),   # 3  Channel (u16 freq + u16 flags)
    (2, 1),   # 4  FHSS
    (1, 1),   # 5  dBm antenna signal (signed)
    (1, 1),   # 6  dBm antenna noise
    (2, 2),   # 7  Lock quality
    (2, 2),   # 8  TX attenuation
    (2, 2),   # 9  dB TX attenuation
    (1, 1),   # 10 dBm TX power
    (1, 1),   # 11 Antenna
    (1, 1),   # 12 dB antenna signal
    (1, 1),   # 13 dB antenna noise
    (2, 2),   # 14 RX flags
)
_RT_FLAG_BAD_FCS = 0x40

DLT_IEEE802_11 = 105
DLT_IEEE802_11_RADIOTAP = 127


def parse_radiotap(buf: bytes) -> tuple[int, int, int, int] | None:
    """Return ``(header_len, freq_mhz, signal_dbm, flags)``.

    freq/signal are 0 when the field is absent. Returns None on a malformed
    header.
    """
    if len(buf) < 8 or buf[0] != 0:
        return None
    hlen = struct.unpack_from("<H", buf, 2)[0]
    if hlen < 8 or hlen > len(buf):
        return None

    pos = 4
    present = struct.unpack_from("<I", buf, pos)[0]
    word = present
    pos += 4
    # Extended present bitmaps chain via bit 31.
    while word & 0x80000000:
        if pos + 4 > hlen:
            break
        word = struct.unpack_from("<I", buf, pos)[0]
        pos += 4

    freq = signal = flags = 0
    off = pos
    for bit, (size, align) in enumerate(_RT_FIELDS):
        if not (present & (1 << bit)):
            continue
        off = (off + align - 1) // align * align
        if off + size > hlen:
            break
        if bit == 1:
            flags = buf[off]
        elif bit == 3:
            freq = struct.unpack_from("<H", buf, off)[0]
        elif bit == 5:
            signal = struct.unpack_from("<b", buf, off)[0]
        off += size
    return hlen, freq, signal, flags


# ── 802.11 management frames ───────────────────────────────────────────────

SUBTYPE_PROBE_RESP = 5
SUBTYPE_BEACON = 8

CAP_ESS = 0x0001
CAP_IBSS = 0x0002
CAP_PRIVACY = 0x0010

_RSN_OUI = b"\x00\x0f\xac"
_WPA_OUI = b"\x00\x50\xf2"

_RSN_CIPHERS = {1: "WEP-40", 2: "TKIP", 4: "CCMP", 5: "WEP-104",
                8: "GCMP", 9: "GCMP-256", 10: "CCMP-256"}
_WPA_CIPHERS = {1: "WEP-40", 2: "TKIP", 4: "CCMP", 5: "WEP-104"}
_RSN_AKMS = {
    1: AKM_EAP, 2: AKM_PSK, 3: AKM_EAP, 4: AKM_PSK, 5: AKM_EAP, 6: AKM_PSK,
    8: AKM_SAE, 9: AKM_SAE, 11: AKM_EAP, 12: AKM_EAP_SUITE_B,
    13: AKM_EAP_SUITE_B, 18: AKM_OWE, 19: AKM_PSK, 20: AKM_SAE,
}
_WPA_AKMS = {1: AKM_EAP, 2: AKM_PSK}


def parse_mgmt_header(frame: bytes):
    """``(subtype, bssid_bytes, capability, ie_bytes)`` for beacon/probe-resp.

    Returns None for anything else — deliberately cheap, this runs on every
    captured frame.
    """
    if len(frame) < 36:
        return None
    fc0 = frame[0]
    if (fc0 >> 2) & 0x3 != 0:                    # not a management frame
        return None
    subtype = (fc0 >> 4) & 0xF
    if subtype not in (SUBTYPE_BEACON, SUBTYPE_PROBE_RESP):
        return None
    bssid = frame[16:22]
    cap = struct.unpack_from("<H", frame, 34)[0]
    return subtype, bssid, cap, frame[36:]


def _parse_suite_block(body: bytes, oui: bytes, ciphers_map: dict,
                       akms_map: dict, pos: int) -> dict:
    """Shared RSN/WPA suite-selector walk (group, pairwise list, AKM list)."""
    ciphers: list[str] = []
    akms: set[str] = set()
    n = len(body)

    def suite_type(at: int) -> int:
        if body[at:at + 3] != oui:
            return -1
        return body[at + 3]

    if pos + 4 <= n:
        t = suite_type(pos)
        pos += 4
        name = ciphers_map.get(t)
        if name:
            ciphers.append(name)
    if pos + 2 <= n:
        count = struct.unpack_from("<H", body, pos)[0]
        pos += 2
        for _ in range(min(count, 16)):
            if pos + 4 > n:
                break
            name = ciphers_map.get(suite_type(pos))
            pos += 4
            if name and name not in ciphers:
                ciphers.append(name)
    if pos + 2 <= n:
        count = struct.unpack_from("<H", body, pos)[0]
        pos += 2
        for _ in range(min(count, 16)):
            if pos + 4 > n:
                break
            akm = akms_map.get(suite_type(pos))
            pos += 4
            if akm:
                akms.add(akm)
    return {"ciphers": ciphers, "akms": akms}


def parse_rsn_ie(body: bytes) -> dict:
    """RSN element (id 48): version, then the suite selectors."""
    if len(body) < 2:
        return {"ciphers": [], "akms": set()}
    return _parse_suite_block(body, _RSN_OUI, _RSN_CIPHERS, _RSN_AKMS, 2)


def parse_wpa_ie(body: bytes) -> dict:
    """WPA vendor element payload, i.e. everything after OUI + type byte."""
    if len(body) < 2:
        return {"ciphers": [], "akms": set()}
    return _parse_suite_block(body, _WPA_OUI, _WPA_CIPHERS, _WPA_AKMS, 2)


def parse_ies(ies: bytes) -> dict:
    """Walk the information elements we care about."""
    out = {"ssid": b"", "channel": 0, "rsn": None, "wpa": None, "wps": False}
    i, n = 0, len(ies)
    while i + 2 <= n:
        eid, ln = ies[i], ies[i + 1]
        i += 2
        if i + ln > n:
            break
        body = ies[i:i + ln]
        i += ln
        if eid == 0:
            out["ssid"] = body
        elif eid == 3 and ln >= 1:
            out["channel"] = body[0]              # DS Parameter Set
        elif eid == 61 and ln >= 1 and not out["channel"]:
            out["channel"] = body[0]              # HT Operation primary channel
        elif eid == 48:
            out["rsn"] = parse_rsn_ie(body)
        elif eid == 221 and ln >= 4 and body[:3] == _WPA_OUI:
            if body[3] == 1:
                out["wpa"] = parse_wpa_ie(body[4:])
            elif body[3] == 4:
                out["wps"] = True
    return out


def decode_ssid(raw: bytes) -> str:
    """SSID bytes → text. Hidden SSIDs (all-zero or empty) become ""."""
    if not raw or not any(raw):
        return ""
    text = raw.decode("utf-8", errors="replace")
    return "".join(ch for ch in text if ch.isprintable() and ch != "\x00")


def describe_bss(cap: int, ies: dict) -> tuple[str, str, int]:
    """``(ssid, auth_bracket_string, channel_from_ds_param)``."""
    auth = build_auth(
        privacy=bool(cap & CAP_PRIVACY),
        ess=not (cap & CAP_IBSS),
        wpa=ies.get("wpa"),
        rsn=ies.get("rsn"),
        wps=bool(ies.get("wps")),
    )
    return decode_ssid(ies.get("ssid") or b""), auth, int(ies.get("channel") or 0)


def _mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


# ── scanner ────────────────────────────────────────────────────────────────

# Frequencies worth hopping across, most-populated first. Intersected with
# what the radio reports as supported before use.
HOP_2G = [2412, 2437, 2462, 2417, 2422, 2427, 2432, 2442, 2447, 2452, 2457,
          2467, 2472]
HOP_5G = [5180, 5200, 5220, 5240, 5745, 5765, 5785, 5805, 5825,
          5260, 5280, 5300, 5320, 5500, 5520, 5540, 5560, 5580, 5600, 5620,
          5640, 5660, 5680, 5700, 5720]
HOP_6G = [5955, 6035, 6115, 6195, 6275, 6355, 6435, 6515, 6595, 6675, 6755,
          6835, 6915, 6995, 7075]

_PCAP_MAGICS = {
    0xA1B2C3D4: ("<", 1_000_000),
    0xD4C3B2A1: (">", 1_000_000),
    0xA1B23C4D: ("<", 1_000_000_000),
    0x4D3CB2A1: (">", 1_000_000_000),
}


class MonitorScanner:
    """Passive beacon/probe-response capture off a monitor interface.

    Drop-in API-compatible with `WifiScanner`: `start()`, `stop()`, `drain()`.
    """

    def __init__(self, iface: str = "wlan1mon", hop: bool = True,
                 dwell_ms: int = 250, emit_interval_s: float = 1.0,
                 snaplen: int = 512, queue_max: int = 256,
                 ie_refresh_s: float = 120.0) -> None:
        self.iface = iface
        self.hop = hop
        self.dwell_s = max(0.05, dwell_ms / 1000.0)
        self.emit_interval_s = emit_interval_s
        self.snaplen = snaplen
        self.ie_refresh_s = ie_refresh_s

        self._q: queue.Queue[list[WifiObs]] = queue.Queue(maxsize=queue_max)
        self._stop = threading.Event()
        self._rx_thr: threading.Thread | None = None
        self._hop_thr: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None

        # BSSID -> (ssid, auth, channel, parsed_at). An AP's identity is
        # stable, so this turns 10 beacons/s/AP into one IE parse per AP.
        self._info: dict[bytes, tuple[str, str, int, float]] = {}
        self._pending: dict[bytes, WifiObs] = {}
        self._pending_lock = threading.Lock()

        self.available = False
        self.last_error: str | None = None
        self.frames: int = 0
        self.bad_fcs: int = 0
        self.dropped_batches: int = 0
        self.current_freq: int = 0
        self.hop_freqs: list[int] = []

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._rx_thr and self._rx_thr.is_alive():
            return
        if not shutil.which("tcpdump"):
            self.last_error = "`tcpdump` not installed (opkg install tcpdump-mini)"
            return
        if not os.path.exists(f"/sys/class/net/{self.iface}"):
            self.last_error = f"{self.iface} not present"
            return
        cmd = [
            "tcpdump", "-i", self.iface, "-w", "-", "-U", "-n",
            "-s", str(self.snaplen),
            "type mgt subtype beacon or type mgt subtype probe-resp",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as e:
            self.last_error = f"spawn: {e}"
            return

        self.available = True
        self._stop.clear()
        self._rx_thr = threading.Thread(target=self._read_loop,
                                        name="mon-rx", daemon=True)
        self._rx_thr.start()
        threading.Thread(target=self._stderr_loop, name="mon-err",
                         daemon=True).start()
        if self.hop:
            self.hop_freqs = self._build_hop_plan()
            if self.hop_freqs:
                self._hop_thr = threading.Thread(target=self._hop_loop,
                                                 name="mon-hop", daemon=True)
                self._hop_thr.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        for thr in (self._rx_thr, self._hop_thr):
            if thr:
                thr.join(timeout=2)
        self._rx_thr = self._hop_thr = None
        if self._proc:
            try:
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self.available = False

    def drain(self) -> list[WifiObs]:
        out: list[WifiObs] = []
        while True:
            try:
                out.extend(self._q.get_nowait())
            except queue.Empty:
                break
        # The in-flight window deliberately stays put: aggregation picks the
        # strongest sighting per AP, and draining it early would split one
        # approach into two weaker rows. `_read_loop` flushes it on shutdown.
        return out

    # ── channel hopping ────────────────────────────────────────────────────

    def _supported_freqs(self) -> set[int]:
        from .wifi import _PHY_FREQ_RE
        try:
            proc = subprocess.run(["iw", "phy"], capture_output=True,
                                  text=True, timeout=10)
        except Exception:
            return set()
        if proc.returncode != 0:
            return set()
        out: set[int] = set()
        for line in proc.stdout.splitlines():
            if "disabled" in line:
                continue
            m = _PHY_FREQ_RE.match(line)
            if m:
                out.add(int(m.group(1)))
        return out

    def _build_hop_plan(self) -> list[int]:
        supported = self._supported_freqs()
        plan: list[int] = []
        for group in (HOP_2G, HOP_5G, HOP_6G):
            for f in group:
                if not supported or f in supported:
                    plan.append(f)
        if not plan:
            return []
        # Revisit 1/6/11 between every few hops — that is where most of what a
        # wardrive logs actually lives.
        anchors = [f for f in (2412, 2437, 2462) if f in plan]
        if not anchors:
            return plan
        woven: list[int] = []
        for i, f in enumerate(plan):
            woven.append(f)
            if i % 3 == 2:
                woven.append(anchors[(i // 3) % len(anchors)])
        return woven

    def _hop_loop(self) -> None:
        freqs = list(self.hop_freqs)
        bad: set[int] = set()
        i = 0
        while not self._stop.is_set() and freqs:
            f = freqs[i % len(freqs)]
            i += 1
            if f in bad:
                continue
            try:
                rc = subprocess.run(["iw", "dev", self.iface, "set", "freq",
                                     str(f)], capture_output=True, timeout=4)
                if rc.returncode != 0:
                    bad.add(f)
                    if len(bad) >= len(freqs):
                        self.last_error = "channel hop rejected on every freq"
                        return
                    continue
                self.current_freq = f
            except Exception:
                bad.add(f)
                continue
            self._stop.wait(self.dwell_s)

    # ── capture ────────────────────────────────────────────────────────────

    # tcpdump chats on stderr even when everything is fine: a startup banner,
    # and on exit a capture summary. Treating those as errors puts a bogus
    # warning on the HUD, so only keep lines that are none of them.
    _TCPDUMP_NOISE = re.compile(
        r"^(tcpdump: (listening|verbose|data link))"
        r"|^\d+\s+packets? (captured|received by filter|dropped by (kernel|interface))"
        r"|^\d+\s+packets? dropped by",
        re.IGNORECASE,
    )

    def _stderr_loop(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode(errors="replace").strip()
                if line and not self._TCPDUMP_NOISE.match(line):
                    self.last_error = line[:120]
        except Exception:
            pass

    def _read_loop(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        fd = proc.stdout.fileno()
        buf = bytearray()
        endian: str | None = None
        ts_div = 1_000_000
        linktype = DLT_IEEE802_11_RADIOTAP
        last_emit = time.monotonic()

        while not self._stop.is_set():
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk

            if endian is None:
                if len(buf) < 24:
                    continue
                magic = struct.unpack_from("<I", buf, 0)[0]
                fmt = _PCAP_MAGICS.get(magic)
                if fmt is None:
                    self.last_error = f"unexpected pcap magic {magic:#x}"
                    return
                endian, ts_div = fmt
                linktype = struct.unpack_from(endian + "I", buf, 20)[0]
                del buf[:24]

            rec_fmt = endian + "IIII"
            while len(buf) >= 16:
                ts_sec, ts_frac, incl, _orig = struct.unpack_from(rec_fmt, buf, 0)
                if incl > 262144:                 # corrupt stream, bail out
                    self.last_error = "pcap record length out of range"
                    return
                if len(buf) < 16 + incl:
                    break
                data = bytes(buf[16:16 + incl])
                del buf[:16 + incl]
                self._handle_packet(data, ts_sec + ts_frac / ts_div, linktype)

            now = time.monotonic()
            if now - last_emit >= self.emit_interval_s:
                self._flush()
                last_emit = now

        self._flush()

    def _handle_packet(self, data: bytes, ts: float, linktype: int) -> None:
        freq = signal = 0
        if linktype == DLT_IEEE802_11_RADIOTAP:
            rt = parse_radiotap(data)
            if rt is None:
                return
            hlen, freq, signal, flags = rt
            if flags & _RT_FLAG_BAD_FCS:
                self.bad_fcs += 1
                return
            frame = data[hlen:]
        else:
            frame = data

        head = parse_mgmt_header(frame)
        if head is None:
            return
        _subtype, bssid, cap, ies_bytes = head
        self.frames += 1

        cached = self._info.get(bssid)
        if cached is None or (ts - cached[3]) > self.ie_refresh_s or not cached[0]:
            ssid, auth, ch = describe_bss(cap, parse_ies(ies_bytes))
            # A hidden beacon must not overwrite a name we already learned
            # from that AP's probe response.
            if cached is not None and not ssid and cached[0]:
                ssid = cached[0]
            self._info[bssid] = (ssid, auth, ch, ts)
        else:
            ssid, auth, ch, _ = cached

        freq, channel = _resolve_channel(freq, ch)

        with self._pending_lock:
            prev = self._pending.get(bssid)
            # Strongest sighting in the window wins — it is the closest
            # approach, which is what trilateration wants.
            if prev is None or signal > prev.rssi:
                self._pending[bssid] = WifiObs(
                    bssid=_mac(bssid), ssid=ssid, channel=channel,
                    frequency=freq, rssi=signal, auth=auth,
                    first_seen=ts, age_s=0.0,
                )

    def _flush(self) -> None:
        with self._pending_lock:
            if not self._pending:
                return
            batch = list(self._pending.values())
            self._pending.clear()
        try:
            self._q.put_nowait(batch)
        except queue.Full:
            # Consumer is behind; drop the oldest window rather than grow
            # without bound. Newer observations are the useful ones.
            try:
                self._q.get_nowait()
                self._q.put_nowait(batch)
            except queue.Empty:
                pass
            except queue.Full:
                pass
            self.dropped_batches += 1


def _channel_to_freq(ch: int) -> int:
    if ch == 14:
        return 2484
    if 1 <= ch <= 13:
        return 2407 + ch * 5
    if 32 <= ch <= 177:
        return 5000 + ch * 5
    return 0


def _resolve_channel(rx_freq: int, ie_channel: int) -> tuple[int, int]:
    """Reconcile where we *listened* with where the AP says it *is*.

    radiotap reports the frequency the monitor interface was tuned to. In
    2.4 GHz the channels overlap, so a beacon from channel 8 is routinely
    heard while the hopper sits on channel 7 — and the row then claims
    "channel 8, 2442 MHz", which is self-contradictory.

    The AP's own DS Parameter Set / HT Operation element is authoritative for
    its channel, so trust it and derive the frequency to match. Only accept it
    when it agrees with the band we received on: a bare channel number is
    ambiguous across 5 and 6 GHz, and guessing wrong would move an AP into the
    wrong band entirely.
    """
    ie_freq = _channel_to_freq(ie_channel) if ie_channel else 0
    if ie_freq and (not rx_freq or _same_band(rx_freq, ie_freq)):
        return ie_freq, ie_channel
    return rx_freq, _freq_to_channel(rx_freq)


def _same_band(a: int, b: int) -> bool:
    return _band_of(a) == _band_of(b)


def _band_of(freq: int) -> int:
    if freq < 2500:
        return 2
    return 6 if freq >= 5900 else 5
