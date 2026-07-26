"""Uploader tests.

Network calls are stubbed at the `urllib.request.urlopen` boundary, so the
multipart body, the v1/v2 routing and the job-polling state machine are all
exercised for real without touching wdgwars.pl.
"""

import gzip
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from . import conftest_path  # noqa: F401
from uploader import wdgwars as api


class FakeResponse(io.BytesIO):
    def __init__(self, payload: dict, status: int = 200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def csv_file(td: str, size_bytes: int = 200, name: str = "wd-test-00.csv") -> Path:
    p = Path(td) / name
    row = "aa:bb:cc:dd:ee:ff,Net,[ESS],2026-01-01 00:00:00,6,2437,-50,1,2,3,4,,0,WIFI\n"
    body = row * max(1, size_bytes // len(row))
    p.write_text("WigleWifi-1.6\nMAC,SSID\n" + body)
    return p


class TestMultipartBody(unittest.TestCase):
    def test_body_contains_the_file_and_boundaries(self):
        with tempfile.TemporaryDirectory() as td:
            csv = csv_file(td)
            tmp, size = api._write_multipart("BOUND", csv)
            try:
                blob = tmp.read_bytes()
                self.assertEqual(size, len(blob))
                self.assertTrue(blob.startswith(b"--BOUND\r\n"))
                self.assertTrue(blob.endswith(b"\r\n--BOUND--\r\n"))
                self.assertIn(csv.name.encode(), blob)
                self.assertIn(b"aa:bb:cc:dd:ee:ff", blob)
            finally:
                tmp.unlink()

    def test_gzip_variant_is_smaller_and_decompresses(self):
        with tempfile.TemporaryDirectory() as td:
            csv = csv_file(td, size_bytes=60000)
            plain, plain_size = api._write_multipart("B", csv)
            gz, gz_size = api._write_multipart("B", csv, gzip_payload=True)
            try:
                self.assertLess(gz_size, plain_size // 4)
                blob = gz.read_bytes()
                self.assertIn(b'filename="' + csv.name.encode() + b'.gz"', blob)
                start = blob.index(b"\r\n\r\n") + 4
                end = blob.rindex(b"\r\n--B--\r\n")
                self.assertEqual(gzip.decompress(blob[start:end]),
                                 csv.read_bytes())
            finally:
                plain.unlink()
                gz.unlink()

    def test_temp_file_lands_next_to_the_csv_not_in_tmpfs(self):
        # /tmp is tmpfs on OpenWrt; staging a 30 MB body there spends RAM.
        with tempfile.TemporaryDirectory() as td:
            csv = csv_file(td)
            tmp, _ = api._write_multipart("B", csv)
            try:
                self.assertEqual(tmp.parent, csv.parent)
            finally:
                tmp.unlink()

    def test_temp_file_is_cleaned_up_after_upload(self):
        with tempfile.TemporaryDirectory() as td:
            csv = csv_file(td)
            with mock.patch.object(api.urllib.request, "urlopen",
                                   return_value=FakeResponse({"ok": True})):
                api.upload_csv("k", csv)
            self.assertEqual(list(Path(td).iterdir()), [csv])


class TestUploadV1(unittest.TestCase):
    def test_success_parses_the_result_breakdown(self):
        payload = {"ok": True, "imported": 305, "captured": 12, "updated": 88,
                   "duplicates": 4, "no_gps": 0, "bad_rows": 1}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(api.urllib.request, "urlopen",
                                   return_value=FakeResponse(payload)):
                res = api.upload_csv("k", csv_file(td))
        self.assertTrue(res.ok)
        self.assertEqual(res.via, "v1")
        self.assertEqual(res.merged_samples, 305)
        self.assertEqual(res.detail["captured"], 12)
        self.assertIn("imported:305", res.summary())
        self.assertIn("bad_rows:1", res.summary())

    def test_http_error_is_surfaced(self):
        err = urllib.error.HTTPError(
            "u", 401, "Unauthorized", {},
            io.BytesIO(json.dumps({"error": "bad key"}).encode()))
        self.addCleanup(err.close)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(api.urllib.request, "urlopen",
                                   side_effect=err):
                res = api.upload_csv("k", csv_file(td))
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 401)
        self.assertEqual(res.error, "bad key")

    def test_offline_is_status_zero(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(
                    api.urllib.request, "urlopen",
                    side_effect=urllib.error.URLError("no route")):
                res = api.upload_csv("k", csv_file(td))
        self.assertEqual(res.status, 0)


class TestUploadV2(unittest.TestCase):
    def _run(self, responses):
        it = iter(responses)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(api.urllib.request, "urlopen",
                                   side_effect=lambda *a, **k: next(it)):
                with mock.patch.object(api.time, "sleep"):
                    return api.upload_csv_v2("k", csv_file(td))

    def test_submits_then_polls_until_done(self):
        res = self._run([
            FakeResponse({"ok": True, "job_id": 42,
                          "poll_url": "/api/v2/upload-job/42"}, 202),
            FakeResponse({"ok": True, "job_id": 42, "status": "queued"}),
            FakeResponse({"ok": True, "job_id": 42, "status": "processing"}),
            FakeResponse({"ok": True, "job_id": 42, "status": "done",
                          "result": {"imported": 900, "captured": 30}}),
        ])
        self.assertTrue(res.ok)
        self.assertEqual(res.via, "v2")
        self.assertEqual(res.job_id, 42)
        self.assertEqual(res.merged_samples, 900)
        self.assertEqual(res.detail["captured"], 30)

    def test_failed_job_is_an_error(self):
        res = self._run([
            FakeResponse({"ok": True, "job_id": 7}, 202),
            FakeResponse({"ok": True, "job_id": 7, "status": "failed",
                          "error": "unparsable header"}),
        ])
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "unparsable header")

    def test_missing_job_id_is_an_error(self):
        res = self._run([FakeResponse({"ok": True}, 202)])
        self.assertFalse(res.ok)
        self.assertIn("job_id", res.error)

    def test_status_callbacks_report_progress(self):
        seen = []
        it = iter([
            FakeResponse({"ok": True, "job_id": 1}, 202),
            FakeResponse({"ok": True, "status": "processing"}),
            FakeResponse({"ok": True, "status": "done", "result": {}}),
        ])
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(api.urllib.request, "urlopen",
                                   side_effect=lambda *a, **k: next(it)):
                with mock.patch.object(api.time, "sleep"):
                    api.upload_csv_v2("k", csv_file(td), on_status=seen.append)
        self.assertTrue(any("packing" in m for m in seen))
        self.assertTrue(any("processing" in m for m in seen))

    def test_transient_poll_failure_is_retried(self):
        it = iter([
            FakeResponse({"ok": True, "job_id": 3}, 202),
            OSError("connection reset"),
            FakeResponse({"ok": True, "status": "done",
                          "result": {"imported": 5}}),
        ])

        def side_effect(*a, **k):
            nxt = next(it)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(api.urllib.request, "urlopen",
                                   side_effect=side_effect):
                with mock.patch.object(api.time, "sleep"):
                    res = api.upload_csv_v2("k", csv_file(td))
        self.assertTrue(res.ok)


class TestJobUrl(unittest.TestCase):
    def test_relative_poll_url(self):
        self.assertEqual(api._job_url(4, "/api/v2/upload-job/4"),
                         "https://wdgwars.pl/api/v2/upload-job/4")

    def test_absolute_poll_url_is_used_verbatim(self):
        self.assertEqual(api._job_url(4, "https://x.test/j/4"),
                         "https://x.test/j/4")

    def test_no_poll_url_falls_back_to_the_documented_path(self):
        self.assertEqual(api._job_url(9, None),
                         "https://wdgwars.pl/api/v2/upload-job/9")


class TestRouting(unittest.TestCase):
    def test_small_files_use_v1(self):
        with tempfile.TemporaryDirectory() as td:
            csv = csv_file(td, size_bytes=1000)
            with mock.patch.object(api, "upload_csv") as v1, \
                 mock.patch.object(api, "upload_csv_v2") as v2:
                v1.return_value = api.UploadResult(True, 200, "{}")
                api.upload_with_retry("k", csv)
        self.assertTrue(v1.called)
        self.assertFalse(v2.called)

    def test_large_files_use_v2(self):
        with tempfile.TemporaryDirectory() as td:
            csv = csv_file(td, size_bytes=1000)
            with mock.patch.object(Path, "stat") as st:
                st.return_value = mock.Mock(st_size=api.V2_THRESHOLD_BYTES + 1)
                with mock.patch.object(api, "upload_csv") as v1, \
                     mock.patch.object(api, "upload_csv_v2") as v2:
                    v2.return_value = api.UploadResult(True, 200, "{}", via="v2")
                    api.upload_with_retry("k", csv)
        self.assertTrue(v2.called)
        self.assertFalse(v1.called)

    def test_mode_can_force_v2(self):
        with tempfile.TemporaryDirectory() as td:
            csv = csv_file(td)
            with mock.patch.object(api, "upload_csv_v2") as v2:
                v2.return_value = api.UploadResult(True, 200, "{}", via="v2")
                api.upload_with_retry("k", csv, mode="v2")
        self.assertTrue(v2.called)

    def test_client_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            csv = csv_file(td)
            with mock.patch.object(api, "upload_csv") as v1:
                v1.return_value = api.UploadResult(False, 401, "", error="bad")
                api.upload_with_retry("k", csv)
        self.assertEqual(v1.call_count, 1)

    def test_gateway_timeout_escalates_to_the_async_queue(self):
        with tempfile.TemporaryDirectory() as td:
            csv = csv_file(td)
            with mock.patch.object(api, "upload_csv") as v1, \
                 mock.patch.object(api, "upload_csv_v2") as v2, \
                 mock.patch.object(api.time, "sleep"):
                v1.return_value = api.UploadResult(False, 504, "", error="gw")
                v2.return_value = api.UploadResult(True, 200, "{}", via="v2")
                res = api.upload_with_retry("k", csv)
        self.assertTrue(res.ok)
        self.assertEqual(res.via, "v2")


class TestUploadHistory(unittest.TestCase):
    PAYLOAD = {
        "ok": True, "count": 1,
        "uploads": [{
            "endpoint": "upload-csv", "filename": "wd-1.csv",
            "file_size": 124516, "created_at": "2026-04-27 00:42:11",
            "result": {"imported": 305, "captured": 12, "updated": 88,
                       "duplicates": 4, "no_gps": 0, "bad_rows": 0},
        }],
    }

    def test_parses_entries(self):
        with mock.patch.object(api.urllib.request, "urlopen",
                               return_value=FakeResponse(self.PAYLOAD)):
            res = api.upload_history("k", limit=5)
        self.assertTrue(res.ok)
        entry = res.uploads[0]
        self.assertEqual(entry.filename, "wd-1.csv")
        self.assertEqual(entry.file_size, 124516)
        self.assertEqual(entry.result["imported"], 305)

    def test_limit_is_clamped_to_the_documented_range(self):
        captured = {}

        def grab(req, **kw):
            captured["url"] = req.full_url
            return FakeResponse(self.PAYLOAD)

        with mock.patch.object(api.urllib.request, "urlopen", side_effect=grab):
            api.upload_history("k", limit=999)
        self.assertIn("limit=50", captured["url"])

    def test_error_response(self):
        with mock.patch.object(api.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")):
            res = api.upload_history("k")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, 0)


class TestResultHelpers(unittest.TestCase):
    def test_merged_prefers_legacy_field(self):
        self.assertEqual(api._merged_from({"merged_samples": 5, "imported": 9}), 5)

    def test_merged_falls_back_to_imported(self):
        self.assertEqual(api._merged_from({"imported": 9}), 9)

    def test_merged_missing(self):
        self.assertEqual(api._merged_from({}), 0)

    def test_summary_without_detail(self):
        self.assertEqual(api.UploadResult(True, 200, "", 7).summary(), "+7")


if __name__ == "__main__":
    unittest.main()
