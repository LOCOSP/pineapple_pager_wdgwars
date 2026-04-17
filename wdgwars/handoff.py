"""APP_HANDOFF protocol — see https://github.com/pineapple-pager-projects/pineapple_pager_loki/blob/main/APP_HANDOFF.md

Each participating payload bundles `launch_<peer>.sh` scripts in its own
directory. We discover them at runtime, present a "JUMP TO" menu, and on
selection write the chosen launcher's path to `data/.next_payload` and exit
with code 42 — the parent `payload.sh` loop re-execs the launcher, skipping
the slow restart of the system pager service.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path

HANDOFF_EXIT_CODE = 42
NEXT_PAYLOAD_FILENAME = ".next_payload"
# Sentinel returned by actions that want to trigger a handoff. The return
# value bubbles up through menu.run → _main_menu → App.run → main(), which
# translates it to `return 42`. No mid-call sys.exit(), so every `finally:`
# block fires in order and the interpreter shuts down cleanly — prevents
# orphan python3 processes holding the LCD / hci0 after the jump.
HANDOFF_SENTINEL = "launch"


@dataclass(frozen=True)
class Launcher:
    title: str
    path: str
    requires: str | None


def discover(payload_dir: str | Path, exclude_basename: str | None = None) -> list[Launcher]:
    """Return launchers found in `payload_dir`, filtered by their `# Requires:` paths."""
    payload_dir = str(payload_dir)
    out: list[Launcher] = []
    for path in sorted(glob.glob(os.path.join(payload_dir, "launch_*.sh"))):
        if exclude_basename and os.path.basename(path) == exclude_basename:
            continue
        title, requires = _read_headers(path)
        if not title:
            continue
        if requires and not os.path.exists(requires):
            continue
        out.append(Launcher(title=title, path=path, requires=requires))
    return out


def request_handoff(payload_dir: str | Path, launcher_path: str) -> str:
    """Write the next-payload marker and return HANDOFF_SENTINEL.

    The caller is expected to bubble the sentinel up to main() which then
    `return 42`s — that lets Python's natural finally-unwind cleanup run
    (GPS stop, pagerctl release, scanner shutdown) before the process exits.
    No direct sys.exit, so no dangling threads/subprocesses.
    """
    data_dir = Path(payload_dir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / NEXT_PAYLOAD_FILENAME).write_text(launcher_path)
    return HANDOFF_SENTINEL


def _read_headers(path: str) -> tuple[str | None, str | None]:
    title = requires = None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 30:
                    break
                if "# Title:" in line:
                    title = line.split("# Title:", 1)[1].strip()
                elif "# Requires:" in line:
                    requires = line.split("# Requires:", 1)[1].strip()
                if title and requires:
                    break
    except OSError:
        return None, None
    return title, requires
