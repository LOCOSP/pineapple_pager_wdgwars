import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (_ROOT / "wdgwars", _ROOT):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
