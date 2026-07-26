"""Wireless interface discovery and selection.

The payload used to hardcode `wlan0`, which is both the pager's own
management radio and the worst possible choice for wardriving: scanning on it
knocks the management AP off channel, and it is the *only* interface used even
when a far better one is present (issue #3 — an external Alfa AWUS036ACM
brought up as `wlan2mon`, for example).

This module enumerates what is actually available and picks a source, so the
app can honour `scan.wifi_iface` from config with a sane `"auto"` default.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# Interfaces the pager's own firmware owns. Scanning on these fights with the
# management AP / client stack, so auto-selection avoids them when it can.
PAGER_OWNED = ("wlan0mgmt", "wlan0open", "wlan0wpa", "wlan0cli", "wlan0ap")

_PHY_RE = re.compile(r"^phy#(\d+)")
_IFACE_RE = re.compile(r"^\s*Interface\s+(\S+)")
_TYPE_RE = re.compile(r"^\s*type\s+(\S+)")


@dataclass(frozen=True)
class IfaceInfo:
    name: str
    type: str          # "managed", "monitor", "AP", ...
    phy: int
    up: bool

    @property
    def is_monitor(self) -> bool:
        return self.type.lower() == "monitor"

    @property
    def is_pager_owned(self) -> bool:
        return self.name in PAGER_OWNED


def list_interfaces() -> list[IfaceInfo]:
    """Parse `iw dev` into structured interface info. Empty list if iw fails."""
    try:
        proc = subprocess.run(["iw", "dev"], capture_output=True, text=True,
                              timeout=10)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return parse_iw_dev(proc.stdout)


def parse_iw_dev(text: str) -> list[IfaceInfo]:
    """Pure parser for `iw dev` output — unit-testable without hardware."""
    out: list[IfaceInfo] = []
    phy = 0
    name: str | None = None
    itype = "managed"
    for line in text.splitlines():
        m = _PHY_RE.match(line)
        if m:
            if name:
                out.append(IfaceInfo(name, itype, phy, _is_up(name)))
                name = None
            phy = int(m.group(1))
            continue
        m = _IFACE_RE.match(line)
        if m:
            if name:
                out.append(IfaceInfo(name, itype, phy, _is_up(name)))
            name = m.group(1)
            itype = "managed"
            continue
        m = _TYPE_RE.match(line)
        if m and name:
            itype = m.group(1)
    if name:
        out.append(IfaceInfo(name, itype, phy, _is_up(name)))
    return out


def _is_up(name: str) -> bool:
    """IFF_UP from sysfs. Monitor interfaces report operstate 'unknown' even
    when live, so the flags bitmask is the only honest signal."""
    try:
        with open(f"/sys/class/net/{name}/flags") as fh:
            return bool(int(fh.read().strip(), 16) & 0x1)
    except OSError:
        return False


def _rank(info: IfaceInfo) -> tuple:
    """Sort key: prefer up, external (higher phy/index), non-pager-owned."""
    idx_m = re.search(r"(\d+)", info.name)
    idx = int(idx_m.group(1)) if idx_m else 0
    return (info.up, not info.is_pager_owned, info.phy, idx)


def pick_wifi_source(preference: str | None = "auto",
                     interfaces: list[IfaceInfo] | None = None,
                     ) -> tuple[str, str, str]:
    """Decide how and where to capture WiFi.

    Returns ``(mode, iface, why)`` where *mode* is ``"monitor"`` or ``"scan"``.

    *preference* accepts an explicit interface name, ``"auto"`` (prefer an
    existing monitor interface, else the best scan-capable one), ``"monitor"``
    or ``"scan"`` to force a backend, or ``"wlan0"``-style legacy values.
    """
    pref = (preference or "auto").strip()
    ifaces = list_interfaces() if interfaces is None else interfaces

    if pref not in ("auto", "monitor", "scan", ""):
        for i in ifaces:
            if i.name == pref:
                mode = "monitor" if i.is_monitor else "scan"
                return mode, i.name, f"configured ({i.type})"
        # Named but not enumerable — trust the user, guess from the name.
        mode = "monitor" if pref.endswith("mon") else "scan"
        return mode, pref, "configured (not in `iw dev`)"

    monitors = sorted((i for i in ifaces if i.is_monitor and i.up),
                      key=_rank, reverse=True)
    scanners = sorted((i for i in ifaces
                       if not i.is_monitor and i.type.lower() in
                       ("managed", "station", "ibss")),
                      key=_rank, reverse=True)

    if pref == "monitor":
        if monitors:
            return "monitor", monitors[0].name, "forced monitor"
        return "scan", (scanners[0].name if scanners else "wlan0"), \
            "monitor requested but none up"

    if pref == "scan":
        return "scan", (scanners[0].name if scanners else "wlan0"), "forced scan"

    # auto
    if monitors:
        return "monitor", monitors[0].name, "monitor iface present"
    if scanners:
        best = scanners[0]
        why = "external adapter" if not best.is_pager_owned and best.phy > 0 \
            else "only scan-capable iface"
        return "scan", best.name, why
    return "scan", "wlan0", "no interfaces enumerated"
