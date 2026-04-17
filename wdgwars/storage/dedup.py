"""TTL-based deduplicator for in-session BSSID/MAC observations."""

from __future__ import annotations

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
