"""Shared builder for the Wigle `AuthMode` bracket string.

Two code paths produce observations — the `iw scan` text parser and the
monitor-mode IE parser — and they must agree byte-for-byte on how a network's
security is spelled, otherwise the same AP looks like two different networks
depending on which backend was running. Both funnel through `build_auth()`.

Layout mirrors Android's `ScanResult.capabilities`, which is what the WiGLE
app itself uploads: WPA1 group, then RSN group, then WPS, then the BSS type.
WPA3 is spelled `[WPA3-SAE-…]` (the wdgwars.pl convention) rather than
Android's `[RSN-SAE-…]`.
"""

from __future__ import annotations

# AKM tokens used by both parsers.
AKM_PSK = "PSK"
AKM_EAP = "EAP"
AKM_SAE = "SAE"
AKM_OWE = "OWE"
AKM_EAP_SUITE_B = "EAP-SUITE-B"

_CIPHER_ORDER = ("CCMP-256", "GCMP-256", "GCMP", "CCMP", "TKIP", "WEP-104", "WEP-40")


def join_ciphers(ciphers) -> str:
    """Deterministic cipher list, e.g. "CCMP+TKIP". Empty falls back to CCMP."""
    if not ciphers:
        return "CCMP"
    seen = [c for c in _CIPHER_ORDER if c in ciphers]
    extra = [c for c in ciphers if c not in _CIPHER_ORDER and c not in seen]
    out = seen + sorted(set(extra))
    return "+".join(out) if out else "CCMP"


def build_auth(*, privacy: bool = False, ess: bool = True,
               wpa: dict | None = None, rsn: dict | None = None,
               wps: bool = False) -> str:
    """Assemble the bracket string.

    *wpa* / *rsn* are ``{"ciphers": [...], "akms": {...}}`` or ``None`` when the
    corresponding element is absent.
    """
    parts: list[str] = []

    if wpa is not None:
        c = join_ciphers(wpa.get("ciphers"))
        akms = set(wpa.get("akms") or ())
        if AKM_EAP in akms or AKM_EAP_SUITE_B in akms:
            parts.append(f"[WPA-EAP-{c}]")
        if AKM_PSK in akms or not akms:
            parts.append(f"[WPA-PSK-{c}]")

    if rsn is not None:
        c = join_ciphers(rsn.get("ciphers"))
        akms = set(rsn.get("akms") or ())
        if AKM_OWE in akms:
            parts.append(f"[WPA3-OWE-{c}]")
        if AKM_SAE in akms:
            parts.append(f"[WPA3-SAE-{c}]")
        if AKM_EAP_SUITE_B in akms:
            parts.append(f"[WPA3-EAP-{c}]")
        elif AKM_EAP in akms:
            parts.append(f"[WPA2-EAP-{c}]")
        # Transition mode advertises PSK *and* SAE. Emitting both keeps the
        # row honest instead of silently downgrading it to WPA3-only.
        if AKM_PSK in akms or not akms & {AKM_SAE, AKM_OWE, AKM_EAP,
                                          AKM_EAP_SUITE_B}:
            parts.append(f"[WPA2-PSK-{c}]")

    if not parts and privacy:
        parts.append("[WEP]")

    if wps:
        parts.append("[WPS]")

    parts.append("[ESS]" if ess else "[IBSS]")
    return "".join(parts)
