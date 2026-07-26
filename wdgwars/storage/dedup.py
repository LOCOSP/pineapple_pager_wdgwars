"""Deduplicators for in-session BSSID/MAC observations.

`TtlDedup` is the original time-only rule: write a MAC at most once per TTL.
It is kept because it is simple and testable, but it is the wrong rule for
wardriving, and in both directions:

* **Parked**, it spams. Forty APs in range with a 60 s TTL writes 2400 rows an
  hour, all from one coordinate, all worthless.
* **Driving**, it starves. Pass an AP in 40 s and you get a single row, when
  trilateration wants three to five sightings from different places.

`GeoDedup` gates on movement instead: a new MAC always writes, and a repeat
writes when you have moved far enough for the sighting to add information,
when the signal got materially stronger (you are closer than you were), or
when so long has passed that a refresh is worth having anyway.
"""

from __future__ import annotations

import math
import time


class TtlDedup:
    def __init__(self, ttl_s: float = 60.0) -> None:
        self.ttl = ttl_s
        self._seen: dict[str, float] = {}

    def should_write(self, kind: str, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        full_key = f"{kind}:{key.lower()}"
        last = self._seen.get(full_key)
        if last is None or (now - last) >= self.ttl:
            self._seen[full_key] = now
            return True
        return False

    def reset(self) -> None:
        self._seen.clear()

    def __len__(self) -> int:
        return len(self._seen)


_M_PER_DEG_LAT = 111320.0


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float,
               cos_lat: float | None = None) -> float:
    """Equirectangular approximation, in metres.

    Haversine would need three trig calls per observation; on a soft-float
    MIPS core with thousands of observations a minute that adds up for no
    benefit. Over the tens-of-metres distances this gates on, the flat-earth
    error is far below GPS noise.
    """
    if cos_lat is None:
        cos_lat = math.cos(math.radians((lat1 + lat2) * 0.5))
    dy = (lat2 - lat1) * _M_PER_DEG_LAT
    dx = (lon2 - lon1) * _M_PER_DEG_LAT * cos_lat
    return math.hypot(dx, dy)


class GeoDedup:
    """Movement-aware deduplicator.

    A repeat sighting is written when any of these hold:
      * you have moved at least *min_move_m* since the last row for that MAC
      * the signal is at least *rssi_delta_db* stronger than last time
      * *ttl_s* has elapsed regardless (a slow refresh so long stops still
        produce the occasional data point)
    """

    def __init__(self, ttl_s: float = 300.0, min_move_m: float = 30.0,
                 rssi_delta_db: int = 6, max_entries: int = 30000,
                 prune_after_s: float = 1800.0) -> None:
        self.ttl = ttl_s
        self.min_move_m = min_move_m
        self.rssi_delta_db = rssi_delta_db
        self.max_entries = max_entries
        self.prune_after_s = prune_after_s
        # key -> (t, lat, lon, rssi)
        self._seen: dict[str, tuple[float, float, float, int]] = {}
        self._cos_lat = 1.0
        self._cos_lat_key: float | None = None

    def _cos(self, lat: float) -> float:
        # cos() changes negligibly over 0.1 degree (~11 km); recompute rarely.
        key = round(lat, 1)
        if key != self._cos_lat_key:
            self._cos_lat_key = key
            self._cos_lat = math.cos(math.radians(lat))
        return self._cos_lat

    def should_write(self, kind: str, key: str, now: float,
                     lat: float, lon: float, rssi: int = 0) -> bool:
        full_key = f"{kind}:{key.lower()}"
        prev = self._seen.get(full_key)
        if prev is None:
            self._maybe_prune(now)
            self._seen[full_key] = (now, lat, lon, rssi)
            return True

        p_t, p_lat, p_lon, p_rssi = prev
        moved = distance_m(p_lat, p_lon, lat, lon, self._cos(lat))
        if (moved >= self.min_move_m
                or (rssi - p_rssi) >= self.rssi_delta_db
                or (now - p_t) >= self.ttl):
            self._seen[full_key] = (now, lat, lon, rssi)
            return True
        return False

    def _maybe_prune(self, now: float) -> None:
        """Drop long-untouched entries once the table gets large.

        A multi-hour drive through a city can see tens of thousands of BSSIDs;
        without this the table only grows, and 256 MB of DDR2 is not a lot.
        """
        if len(self._seen) < self.max_entries:
            return
        cutoff = now - self.prune_after_s
        stale = [k for k, v in self._seen.items() if v[0] < cutoff]
        for k in stale:
            del self._seen[k]
        if len(self._seen) >= self.max_entries:
            # Nothing was stale enough — evict the oldest quarter instead.
            ordered = sorted(self._seen.items(), key=lambda kv: kv[1][0])
            for k, _ in ordered[:len(ordered) // 4]:
                del self._seen[k]

    def reset(self) -> None:
        self._seen.clear()

    def __len__(self) -> int:
        return len(self._seen)
