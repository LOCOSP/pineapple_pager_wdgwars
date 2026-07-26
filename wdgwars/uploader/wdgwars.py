"""HTTP client for wdgwars.pl — multipart CSV upload + key validation.

Two upload paths, matching the server API:

* **v1** `POST /api/upload-csv` — synchronous, the response carries the import
  result. Fine for the few-hundred-kilobyte files a normal session produces.
* **v2** `POST /api/v2/upload-csv` — returns `202` with a `job_id`, the parse
  runs server-side, the client polls `GET /api/v2/upload-job/<id>`. Meant for
  large files and slow links, which is exactly a pager tethered to a phone at
  the end of a long drive. v2 also accepts `.gz`, and a WiGLE CSV compresses
  roughly eight-fold, so v2 uploads are gzipped.

Both paths stream the request body from a temporary file rather than
assembling it in memory. The old code did `csv_path.read_bytes()` and then
concatenated head + payload + tail, which peaks at twice the file size — 60 MB
of transient allocation on a 256 MB device for a rotated 30 MB session.
"""

from __future__ import annotations

import gzip
import json
import mimetypes
import os
import shutil
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

API_BASE = "https://wdgwars.pl/api"
API_V2 = "https://wdgwars.pl/api/v2"
USER_AGENT = "wdgwars-pager/1.1 (+hak5)"
RATE_LIMIT_SLEEP_S = 2.5
RETRY_DELAYS_S = (2.0, 8.0, 30.0)

# Above this the synchronous endpoint starts risking an HTTP timeout on a
# tethered link, so hand the file to the async queue instead.
V2_THRESHOLD_BYTES = 20 * 1024 * 1024
V2_POLL_INTERVAL_S = 3.0
V2_POLL_TIMEOUT_S = 900.0
GZIP_LEVEL = 6

# Server-side cap on the uploaded artefact.
MAX_UPLOAD_BYTES = 30 * 1024 * 1024


@dataclass
class UploadResult:
    ok: bool
    status: int
    body: str
    merged_samples: int = 0
    error: str | None = None
    via: str = "v1"
    job_id: int | None = None
    # Full server breakdown: imported / captured / updated / duplicates /
    # no_gps / bad_rows / cooldown. `no_gps` is worth watching — it is the
    # server's count of rows we sent without usable coordinates.
    detail: dict = field(default_factory=dict)

    def summary(self) -> str:
        d = self.detail
        if not d:
            return f"+{self.merged_samples}"
        bits = [f"{k}:{d[k]}" for k in
                ("imported", "captured", "updated", "duplicates", "no_gps",
                 "bad_rows")
                if d.get(k)]
        return "  ".join(bits) or f"+{self.merged_samples}"


@dataclass
class HistoryEntry:
    endpoint: str
    filename: str
    file_size: int
    created_at: str
    result: dict = field(default_factory=dict)


@dataclass
class HistoryResult:
    ok: bool
    status: int
    uploads: list = field(default_factory=list)
    error: str | None = None


@dataclass
class MeResult:
    ok: bool
    status: int
    body: str
    username: str = ""
    wifi: int = 0
    ble: int = 0
    aircraft: int = 0
    mesh: int = 0
    total: int = 0
    gang: str = ""
    badges: list[str] = None
    error: str | None = None


def me(api_key: str, timeout: float = 15.0) -> MeResult:
    if not api_key:
        return MeResult(ok=False, status=0, body="", error="empty api key")
    req = urllib.request.Request(
        f"{API_BASE}/me",
        headers={"X-API-Key": api_key, "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            obj = _safe_json(data)
            return MeResult(
                ok=bool(obj.get("ok")),
                status=resp.status,
                body=data,
                username=obj.get("username", ""),
                wifi=int(obj.get("wifi", 0)),
                ble=int(obj.get("ble", 0)),
                aircraft=int(obj.get("aircraft", 0)),
                mesh=int(obj.get("mesh", 0)),
                total=int(obj.get("total", 0)),
                gang=obj.get("gang", ""),
                badges=obj.get("badges", []) or [],
                error=None if obj.get("ok") else obj.get("error", "unknown"),
            )
    except urllib.error.HTTPError as e:
        body = _read_err(e)
        obj = _safe_json(body)
        return MeResult(ok=False, status=e.code, body=body,
                        error=obj.get("error", e.reason))
    except urllib.error.URLError as e:
        return MeResult(ok=False, status=0, body="", error=str(e.reason))
    except Exception as e:
        return MeResult(ok=False, status=0, body="", error=f"{type(e).__name__}: {e}")


# ── v1: synchronous ────────────────────────────────────────────────────────

def upload_csv(api_key: str, csv_path: Path, timeout: float = 120.0) -> UploadResult:
    boundary = "----wdgwars" + uuid.uuid4().hex
    tmp = None
    try:
        tmp, length = _write_multipart(boundary, csv_path, gzip_payload=False)
        with tmp.open("rb") as body:
            req = urllib.request.Request(
                f"{API_BASE}/upload-csv",
                data=body,
                headers={
                    "X-API-Key": api_key,
                    "User-Agent": USER_AGENT,
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(length),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read().decode("utf-8", errors="replace")
                obj = _safe_json(data)
                return UploadResult(ok=True, status=resp.status, body=data,
                                    merged_samples=_merged_from(obj), via="v1",
                                    detail=_detail_from(obj))
    except urllib.error.HTTPError as e:
        body = _read_err(e)
        obj = _safe_json(body)
        return UploadResult(ok=False, status=e.code, body=body,
                            error=obj.get("error", e.reason), via="v1")
    except urllib.error.URLError as e:
        return UploadResult(ok=False, status=0, body="", error=str(e.reason),
                            via="v1")
    except Exception as e:
        return UploadResult(ok=False, status=0, body="",
                            error=f"{type(e).__name__}: {e}", via="v1")
    finally:
        _unlink(tmp)


# ── v2: async queue ────────────────────────────────────────────────────────

def upload_csv_v2(api_key: str, csv_path: Path, timeout: float = 180.0,
                  poll_timeout: float = V2_POLL_TIMEOUT_S,
                  on_status: Callable[[str], None] | None = None,
                  gzip_payload: bool = True) -> UploadResult:
    """Submit to the async queue, then poll the job until it settles."""
    boundary = "----wdgwars" + uuid.uuid4().hex
    tmp = None
    try:
        if on_status:
            on_status("packing…" if gzip_payload else "preparing…")
        tmp, length = _write_multipart(boundary, csv_path,
                                       gzip_payload=gzip_payload)
        if length > MAX_UPLOAD_BYTES:
            return UploadResult(ok=False, status=413, body="",
                                error=f"body {length // (1 << 20)}MB over server cap",
                                via="v2")
        if on_status:
            on_status(f"sending {length // 1024}k…")
        with tmp.open("rb") as body:
            req = urllib.request.Request(
                f"{API_V2}/upload-csv",
                data=body,
                headers={
                    "X-API-Key": api_key,
                    "User-Agent": USER_AGENT,
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(length),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read().decode("utf-8", errors="replace")
                obj = _safe_json(data)
    except urllib.error.HTTPError as e:
        body = _read_err(e)
        obj = _safe_json(body)
        return UploadResult(ok=False, status=e.code, body=body,
                            error=obj.get("error", e.reason), via="v2")
    except urllib.error.URLError as e:
        return UploadResult(ok=False, status=0, body="", error=str(e.reason),
                            via="v2")
    except Exception as e:
        return UploadResult(ok=False, status=0, body="",
                            error=f"{type(e).__name__}: {e}", via="v2")
    finally:
        _unlink(tmp)

    job_id = obj.get("job_id")
    if not obj.get("ok") or job_id is None:
        return UploadResult(ok=False, status=202, body=json.dumps(obj),
                            error=obj.get("error", "no job_id in response"),
                            via="v2")
    return poll_job(api_key, int(job_id), obj.get("poll_url"),
                    poll_timeout=poll_timeout, on_status=on_status)


def poll_job(api_key: str, job_id: int, poll_url: str | None = None,
             poll_timeout: float = V2_POLL_TIMEOUT_S,
             interval_s: float = V2_POLL_INTERVAL_S,
             on_status: Callable[[str], None] | None = None) -> UploadResult:
    """Poll an upload job until it reaches `done` or `failed`."""
    url = _job_url(job_id, poll_url)
    deadline = time.monotonic() + poll_timeout
    last_state = ""
    while True:
        try:
            req = urllib.request.Request(
                url, headers={"X-API-Key": api_key, "User-Agent": USER_AGENT},
                method="GET")
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                data = resp.read().decode("utf-8", errors="replace")
                obj = _safe_json(data)
                status_code = resp.status
        except urllib.error.HTTPError as e:
            body = _read_err(e)
            return UploadResult(ok=False, status=e.code, body=body,
                                error=_safe_json(body).get("error", e.reason),
                                via="v2", job_id=job_id)
        except Exception as e:
            # A dropped poll is not a failed import — the job keeps running
            # server-side, so retry until the deadline.
            if time.monotonic() >= deadline:
                return UploadResult(ok=False, status=0, body="",
                                    error=f"poll failed: {e}", via="v2",
                                    job_id=job_id)
            time.sleep(interval_s)
            continue

        state = str(obj.get("status", "")).lower()
        if state != last_state and on_status:
            on_status(f"job {job_id}: {state or 'unknown'}")
            last_state = state

        if state == "done":
            result = obj.get("result") or {}
            return UploadResult(ok=True, status=status_code, body=data,
                                merged_samples=_merged_from(result),
                                via="v2", job_id=job_id,
                                detail=_detail_from(result))
        if state == "failed":
            return UploadResult(
                ok=False, status=status_code, body=data,
                error=(obj.get("error") or (obj.get("result") or {}).get("error")
                       or "job failed"),
                via="v2", job_id=job_id)

        if time.monotonic() >= deadline:
            return UploadResult(ok=False, status=status_code, body=data,
                                error=f"job still {state or 'pending'} after "
                                      f"{int(poll_timeout)}s",
                                via="v2", job_id=job_id)
        time.sleep(interval_s)


def _job_url(job_id: int, poll_url: str | None) -> str:
    if poll_url:
        if poll_url.startswith("http://") or poll_url.startswith("https://"):
            return poll_url
        return "https://wdgwars.pl" + (
            poll_url if poll_url.startswith("/") else "/" + poll_url)
    return f"{API_V2}/upload-job/{job_id}"


# ── routing + retry ────────────────────────────────────────────────────────

def upload_with_retry(api_key: str, csv_path: Path,
                      on_attempt: Callable[[int, str], None] | None = None,
                      mode: str = "auto") -> UploadResult:
    """Upload with backoff. *mode* is "auto", "v1" or "v2".

    "auto" sends small files down the synchronous path (one round-trip, one
    answer) and hands anything large to the async queue, where an HTTP
    timeout on a flaky tether no longer costs the whole upload.
    """
    try:
        size = csv_path.stat().st_size
    except OSError:
        size = 0
    use_v2 = mode == "v2" or (mode == "auto" and size >= V2_THRESHOLD_BYTES)

    last: UploadResult | None = None
    for attempt, delay in enumerate(RETRY_DELAYS_S, start=1):
        def status(msg: str, _a=attempt) -> None:
            if on_attempt:
                on_attempt(_a, msg)

        status(f"upload {csv_path.name} (try {attempt}"
               f"{', v2' if use_v2 else ''})")
        if use_v2:
            last = upload_csv_v2(api_key, csv_path, on_status=status)
        else:
            last = upload_csv(api_key, csv_path)
        if last.ok:
            return last
        # Don't retry on client errors that won't change
        if last.status in (400, 401, 403, 413, 415):
            return last
        # A body the sync endpoint choked on may still go through the queue.
        if not use_v2 and last.status in (0, 408, 502, 503, 504) and size > 0:
            use_v2 = True
        if attempt < len(RETRY_DELAYS_S):
            time.sleep(delay)
    return last  # type: ignore[return-value]


# ── multipart body ─────────────────────────────────────────────────────────

def _write_multipart(boundary: str, csv_path: Path,
                     gzip_payload: bool = False) -> tuple[Path, int]:
    """Write the full request body to a sibling temp file; return (path, size).

    The temp file lives next to the CSV on purpose: `/tmp` is tmpfs on
    OpenWrt, so staging a 30 MB body there would spend the device's RAM.
    """
    filename = csv_path.name + (".gz" if gzip_payload else "")
    ctype = ("application/gzip" if gzip_payload
             else (mimetypes.guess_type(csv_path.name)[0] or "text/csv"))
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")

    # Unique per call — a retry or a second body for the same CSV must not
    # scribble over one still being read.
    tmp = csv_path.with_suffix(
        csv_path.suffix + f".up{os.getpid()}-{uuid.uuid4().hex[:8]}")
    with tmp.open("wb") as out:
        out.write(head)
        with csv_path.open("rb") as src:
            if gzip_payload:
                with gzip.GzipFile(filename=csv_path.name, mode="wb",
                                   fileobj=out, compresslevel=GZIP_LEVEL,
                                   mtime=0) as gz:
                    shutil.copyfileobj(src, gz, length=1 << 16)
            else:
                shutil.copyfileobj(src, out, length=1 << 16)
        out.write(tail)
    return tmp, tmp.stat().st_size


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def upload_history(api_key: str, limit: int = 10,
                   timeout: float = 20.0) -> HistoryResult:
    """`GET /api/upload-history` — what the server made of past uploads.

    Worth having on-device: it is the only way to see the server's own
    `no_gps` / `bad_rows` / `duplicates` counts, i.e. whether what the pager
    is producing is actually landing.
    """
    limit = max(1, min(50, int(limit)))
    req = urllib.request.Request(
        f"{API_BASE}/upload-history?limit={limit}",
        headers={"X-API-Key": api_key, "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = _safe_json(resp.read().decode("utf-8", errors="replace"))
            entries = [
                HistoryEntry(
                    endpoint=str(u.get("endpoint", "")),
                    filename=str(u.get("filename", "")),
                    file_size=int(u.get("file_size", 0) or 0),
                    created_at=str(u.get("created_at", "")),
                    result=u.get("result") or {},
                )
                for u in (obj.get("uploads") or [])
            ]
            return HistoryResult(ok=bool(obj.get("ok")), status=resp.status,
                                 uploads=entries,
                                 error=None if obj.get("ok") else obj.get("error"))
    except urllib.error.HTTPError as e:
        body = _read_err(e)
        return HistoryResult(ok=False, status=e.code,
                             error=_safe_json(body).get("error", e.reason))
    except urllib.error.URLError as e:
        return HistoryResult(ok=False, status=0, error=str(e.reason))
    except Exception as e:
        return HistoryResult(ok=False, status=0,
                             error=f"{type(e).__name__}: {e}")


_DETAIL_KEYS = ("imported", "captured", "updated", "duplicates", "no_gps",
                "bad_rows", "cooldown")


def _detail_from(obj: dict) -> dict:
    out = {}
    for key in _DETAIL_KEYS:
        if key in obj:
            try:
                out[key] = int(obj[key])
            except (TypeError, ValueError):
                pass
    return out


def _merged_from(obj: dict) -> int:
    """Row count from an import result, whichever field the server used."""
    for key in ("merged_samples", "imported", "captured", "updated"):
        if key in obj:
            try:
                return int(obj[key])
            except (TypeError, ValueError):
                continue
    return 0


def _safe_json(text: str) -> dict:
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _read_err(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
