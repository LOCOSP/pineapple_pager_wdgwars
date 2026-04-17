"""HTTP client for wdgwars.pl — multipart CSV upload + key validation."""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

API_BASE = "https://wdgwars.pl/api"
USER_AGENT = "wdgwars-pager/1.0 (+hak5)"
RATE_LIMIT_SLEEP_S = 2.5
RETRY_DELAYS_S = (2.0, 8.0, 30.0)


@dataclass
class UploadResult:
    ok: bool
    status: int
    body: str
    merged_samples: int = 0
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


def upload_csv(api_key: str, csv_path: Path, timeout: float = 60.0) -> UploadResult:
    boundary = "----wdgwars" + uuid.uuid4().hex
    body = _build_multipart(boundary, csv_path)
    req = urllib.request.Request(
        f"{API_BASE}/upload-csv",
        data=body,
        headers={
            "X-API-Key": api_key,
            "User-Agent": USER_AGENT,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            obj = _safe_json(data)
            return UploadResult(
                ok=True,
                status=resp.status,
                body=data,
                merged_samples=int(obj.get("merged_samples", 0)),
            )
    except urllib.error.HTTPError as e:
        body = _read_err(e)
        obj = _safe_json(body)
        return UploadResult(
            ok=False, status=e.code, body=body,
            error=obj.get("error", e.reason),
        )
    except urllib.error.URLError as e:
        return UploadResult(ok=False, status=0, body="", error=str(e.reason))
    except Exception as e:
        return UploadResult(ok=False, status=0, body="", error=f"{type(e).__name__}: {e}")


def upload_with_retry(api_key: str, csv_path: Path,
                      on_attempt: Callable[[int, str], None] | None = None) -> UploadResult:
    last: UploadResult | None = None
    for attempt, delay in enumerate(RETRY_DELAYS_S, start=1):
        if on_attempt:
            on_attempt(attempt, f"upload {csv_path.name} (try {attempt})")
        last = upload_csv(api_key, csv_path)
        if last.ok:
            return last
        # Don't retry on client errors that won't change
        if last.status in (400, 401, 403, 413, 415):
            return last
        if attempt < len(RETRY_DELAYS_S):
            time.sleep(delay)
    return last  # type: ignore[return-value]


def _build_multipart(boundary: str, csv_path: Path) -> bytes:
    filename = csv_path.name
    ctype = mimetypes.guess_type(filename)[0] or "text/csv"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8")
    with csv_path.open("rb") as f:
        payload = f.read()
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + payload + tail


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


_ = os  # silence unused-import lint
