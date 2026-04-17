"""Add wdgwars/ to sys.path for tests run from the repo root."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PKG = _ROOT / "wdgwars"
for p in (str(_PKG), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
