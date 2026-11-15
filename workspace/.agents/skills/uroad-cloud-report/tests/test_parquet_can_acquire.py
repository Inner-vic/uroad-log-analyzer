"""Offline acquisition/cache boundary tests, never using a business endpoint."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1]/"scripts"
sys.path.insert(0, str(SCRIPTS))
import parquet_can_signals as wrapper

RUN_ID = "run-20261103-120000-000001-abcdef01"


class AcquireTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/RUN_ID
        self.root.mkdir()
        self.work = self.root/"slices/0001/can-parquet"
        self.output = self.root/"slices/0001/analysis/can-signals.json"
        self.args = SimpleNamespace(vin="DEM0VEH1CLEX00001", start="2026-11-02 17:29:00",
            end="2026-11-02 18:04:00", output=str(self.output), work_dir=str(self.work), run_id=RUN_ID,
            cloud_env="prod", page_size=37, max_files=None)
        self.items = [{"fileKey": f"key-{number}", "fileName": "same-name.parquet", "collectTime": str(number),
                       "downloadURL": f"https://download.invalid/object/{number}?private-token=secret"} for number in (2, 1)]
        self.build = Mock(side_effect=self.fake_build)
        self.loader = Mock(return_value={"sourceParquet": {"rows": 8}})
        stack = ExitStack(); self.addCleanup(stack.close)
        self.query = stack.enter_context(patch.object(wrapper, "query_files", return_value=SimpleNamespace(
            items=self.items, total=2, environment="prod", provider_environment="prod")))
        stack.enter_context(patch.object(wrapper, "load_settings", return_value={"username": "private-user"}))
        stack.enter_context(patch.object(wrapper, "environment_config", return_value={"providerEnvironment": "prod"}))
        self.download = stack.enter_context(patch.object(wrapper, "download_to_file", side_effect=self.fake_download))
        stack.enter_context(patch.object(wrapper, "_backend", return_value=SimpleNamespace(
            build_contract=self.build, load_indexed_contract=self.loader)))

    def fake_download(self, url, source, *, environment):
        self.assertEqual(environment, "prod")
        source.write_bytes(url.encode())

    def fake_build(self, paths, definitions, start, end, output, index, metadata, binding, **kwargs):
        self.assertEqual([path.read_bytes() for path in paths], [item["downloadURL"].encode() for item in self.items[:len(paths)]])
        self.assertEqual(kwargs["batch_size"], 4096)
        self.assertEqual(index, self.work/"points.sqlite3")
        kwargs["progress"]({"stage": "reading_parquet", "filesRead": len(paths), "rowsSeen": 8,
                            "pointsSelected": 4, "uniquePoints": 3})
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"synthetic":true}')
        index.write_bytes(b"synthetic-index")
        metadata.write_text('{"synthetic-receipt":true}')
        return {"rows": 8}

    def fail_decode(self, *_args, **_kwargs):
        (self.work/"points.sqlite3.building-failed").write_bytes(b"preserved-partial")
        raise MemoryError()

    def fail_first(self):
        self.build.side_effect = self.fail_decode
        with self.assertRaisesRegex(RuntimeError, "内存不足"):
            wrapper.acquire(self.args)

    def test_query_options_original_order_and_same_basename_isolation(self):
        result = wrapper.acquire(self.args)
        self.assertEqual(result, {"ok": True, "files": 2, "rows": 8})
        self.assertEqual(self.query.call_args.kwargs["page_size"], 37)
        sources = self.build.call_args.args[0]
        self.assertNotEqual(sources[0], sources[1])
        self.assertTrue(all(path.is_file() for path in sources))
        progress = json.loads((self.work/"progress.json").read_text())
        wrapper.validate_progress(progress)
        self.assertEqual(progress["stage"], "completed")
        self.assertNotIn("secret", json.dumps(progress))
        self.assertNotIn("vin", progress)

    def test_file_limit_applies_before_download_and_is_frozen(self):
        self.args.max_files = 1
        wrapper.acquire(self.args)
        self.assertEqual(self.download.call_count, 1)
        self.assertEqual(len(self.build.call_args.args[0]), 1)
        snapshot = json.loads((self.work/"sources.json").read_text())
        self.assertEqual(snapshot["options"]["maxFiles"], 1)

    def test_incomplete_download_retries_same_frozen_url_then_records_receipt(self):
        attempts = {}
        def interrupted(url, source, *, environment):
            attempts[url] = attempts.get(url, 0) + 1
            if attempts[url] == 1:
                raise IncompleteRead(b'partial', 10)
            return self.fake_download(url, source, environment=environment)
        self.download.side_effect = interrupted
        with patch.object(wrapper.time, 'sleep') as sleep:
            wrapper.acquire(self.args)
        self.assertEqual(self.query.call_count, 1)
        self.assertEqual(list(attempts.values()), [2, 2])
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(json.loads(p.read_text())['downloadAttempts'] == 2 for p in self.work.glob('downloads/*/receipt.json')))

    def test_exhausted_network_retry_preserves_cache_and_resume_downloads_only_missing(self):
        def interrupted(url, source, *, environment):
            if url == self.items[1]['downloadURL']:
                raise IncompleteRead(b'partial', 10)
            return self.fake_download(url, source, environment=environment)
        self.download.side_effect = interrupted
        with patch.object(wrapper.time, 'sleep'), self.assertRaises(RuntimeError) as caught:
            wrapper.acquire(self.args)
        self.assertEqual(caught.exception.error_type, 'IncompleteRead')
        self.assertEqual(self.download.call_count, 4)
        self.assertEqual(len(list(self.work.glob('downloads/*/receipt.json'))), 1)
        self.query.reset_mock(); self.download.reset_mock(); self.download.side_effect = self.fake_download
        wrapper.acquire(self.args)
        self.query.assert_not_called()
        self.assertEqual(self.download.call_count, 1)
        self.assertEqual(self.download.call_args.args[0], self.items[1]['downloadURL'])

    def test_auth_failure_is_not_automatically_retried(self):
        from cheyun_prod_client import CloudAuthError
        self.download.side_effect = CloudAuthError('private credential detail')
        with patch.object(wrapper.time, 'sleep') as sleep, self.assertRaises(RuntimeError):
            wrapper.acquire(self.args)
        self.assertEqual(self.download.call_count, 1)
        sleep.assert_not_called()

    def test_query_without_filekey_is_bound_by_complete_record_snapshot_and_receipts(self):
        for item in self.items:
            item.pop("fileKey")
        wrapper.acquire(self.args)
        frozen = json.loads((self.work/"sources.json").read_text())
        self.assertEqual(frozen["items"], self.items)
        for number, receipt_path in enumerate(sorted(self.work.glob("downloads/*/receipt.json"))):
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt["record_sha256"], wrapper._digest(self.items[number]))
            self.assertEqual(receipt["snapshot_sha256"], frozen["snapshot_sha256"])
        self.query.reset_mock(); self.download.reset_mock(); self.build.reset_mock()
        wrapper.acquire(self.args)
        self.query.assert_not_called(); self.download.assert_not_called(); self.build.assert_not_called()
        self.loader.assert_called_once()

    def test_missing_name_or_url_is_rejected_before_downloading(self):
        for number, field in enumerate(("fileName", "downloadURL")):
            item = dict(self.items[0]); item.pop(field)
            self.query.return_value.items = [item]
            changed = copy.copy(self.args)
            changed.work_dir = str(self.root/"slices/0001"/f"bad-source-{number}")
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                wrapper.acquire(changed)
        self.download.assert_not_called(); self.build.assert_not_called()

    def test_resume_reuses_verified_downloads_without_requery_and_archives_partial(self):
        self.fail_first()
        self.query.reset_mock(); self.download.reset_mock()
        self.build.side_effect = self.fake_build
        wrapper.acquire(self.args)
        self.query.assert_not_called(); self.download.assert_not_called()
        partials = list(self.work.glob("failed-decode-*/points.sqlite3.building-failed"))
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].read_bytes(), b"preserved-partial")

    def test_success_receipt_is_loaded_without_rebuilding_or_overwriting(self):
        wrapper.acquire(self.args)
        before = self.output.read_bytes()
        self.build.reset_mock(); self.query.reset_mock(); self.download.reset_mock()
        self.assertEqual(wrapper.acquire(self.args)["rows"], 8)
        self.loader.assert_called_once_with(self.output, self.work/"points.sqlite3", self.work/"contract-metadata.json",
            {"runId": RUN_ID, "vin": self.args.vin, "start": self.args.start, "end": self.args.end,
             "environment": "prod", "providerEnvironment": "prod"}, wrapper.DEFINITIONS)
        self.build.assert_not_called(); self.query.assert_not_called(); self.download.assert_not_called()
        self.assertEqual(self.output.read_bytes(), before)

    def test_changed_input_environment_or_options_rejected_without_mutating_old_progress(self):
        self.fail_first()
        before = (self.work/"progress.json").read_bytes()
        for field, value in (("vin", "DEM0VEH1CLEX00002"), ("start", "2026-11-02 17:30:00"),
                             ("cloud_env", "test"), ("page_size", 100), ("max_files", 1)):
            changed = copy.copy(self.args); setattr(changed, field, value)
            self.query.reset_mock(); self.download.reset_mock()
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                wrapper.acquire(changed)
            self.query.assert_not_called(); self.download.assert_not_called()
            self.assertEqual((self.work/"progress.json").read_bytes(), before)

    def test_missing_or_changed_download_receipt_is_rejected(self):
        self.fail_first()
        receipts = list(self.work.glob("downloads/*/receipt.json"))
        receipt = receipts[0]
        payload = json.loads(receipt.read_text())
        payload["sha256"] = "0"*64
        receipt.write_text(json.dumps(payload))
        self.query.reset_mock(); self.download.reset_mock()
        with self.assertRaises(RuntimeError):
            wrapper.acquire(self.args)
        self.query.assert_not_called(); self.download.assert_not_called()
        receipt.unlink()
        with self.assertRaises(RuntimeError):
            wrapper.acquire(self.args)
        self.download.assert_not_called()

    def test_old_workdir_without_snapshot_is_not_requeried(self):
        self.work.mkdir(parents=True)
        (self.work/"unknown.parquet").write_bytes(b"unowned")
        with self.assertRaises(RuntimeError):
            wrapper.acquire(self.args)
        self.query.assert_not_called()

    def test_changed_definitions_or_invalid_progress_are_rejected(self):
        self.fail_first()
        different = self.root/"changed-definitions.json"
        different.write_text('{}')
        self.query.reset_mock()
        with patch.object(wrapper, "DEFINITIONS", different), self.assertRaises(RuntimeError):
            wrapper.acquire(self.args)
        self.query.assert_not_called()
        value = json.loads((self.work/"progress.json").read_text())
        for change in ({"stage": []}, {"rows_seen": True}, {"elapsed_seconds": float("nan")}, {"url": "private"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                wrapper.validate_progress(dict(value, **change))

    def test_invalid_input_never_queries_or_creates_workdir(self):
        for field, value in (("vin", "not-a-vin"), ("end", self.args.start), ("start", "unknown")):
            changed = copy.copy(self.args); setattr(changed, field, value)
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                wrapper.acquire(changed)
        self.query.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_cross_task_output_and_forged_snapshot_are_rejected(self):
        foreign = copy.copy(self.args)
        foreign.output = str(self.root.parent/"other-task/can-signals.json")
        with self.assertRaises(RuntimeError):
            wrapper.acquire(foreign)
        self.query.assert_not_called()
        self.fail_first()
        snapshot_path = self.work/"sources.json"
        snapshot = json.loads(snapshot_path.read_text())
        snapshot["items"][0]["fileKey"] = "foreign-key"
        snapshot_path.write_text(json.dumps(snapshot))
        self.query.reset_mock()
        with self.assertRaises(RuntimeError):
            wrapper.acquire(self.args)
        self.query.assert_not_called()

    def test_orphan_output_is_not_overwritten(self):
        self.fail_first()
        self.output.parent.mkdir(parents=True)
        self.output.write_text("unknown-success-evidence")
        self.build.reset_mock()
        with self.assertRaises(RuntimeError):
            wrapper.acquire(self.args)
        self.build.assert_not_called()
        self.assertEqual(self.output.read_text(), "unknown-success-evidence")

    def test_memory_error_cli_has_nonempty_safe_stderr_and_private_trace(self):
        self.build.side_effect = MemoryError()
        argv = ["parquet_can_signals", "--vin", self.args.vin, "--start", self.args.start, "--end", self.args.end,
                "--output", str(self.output), "--work-dir", str(self.work), "--run-id", RUN_ID]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(wrapper.main(), 1)
        payload = json.loads(err.getvalue())
        self.assertEqual(payload["errorType"], "MemoryError")
        self.assertIn("内存不足", payload["error"])
        self.assertEqual(payload["stage"], "decoding")
        self.assertNotIn("secret", err.getvalue())
        self.assertEqual(out.getvalue(), "")
        self.assertTrue((self.work/"diagnostic.json").is_file())
        self.assertTrue(list(self.work.glob("downloads/*/receipt.json")))

    def test_real_batch_decoder_publishes_and_reloads_cross_parent_paths(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("PyArrow unavailable in this test interpreter")
        import parquet_can_stream
        def download(_url, source, *, environment):
            pq.write_table(pa.Table.from_pylist([
                {"timestamp": "2026-11-02T17:30:00+08:00", "can_id": 0x12A, "channel": 2, "data": bytes(32)},
                {"timestamp": "2026-11-02T17:30:01+08:00", "can_id": 0x108, "channel": 1, "data": bytes(32)},
                {"timestamp": "2026-11-02T17:30:02+08:00", "can_id": 0x106, "channel": 2, "data": bytes(16)}]), source)
        with patch.object(wrapper, "download_to_file", side_effect=download), \
                patch.object(wrapper, "_backend", return_value=parquet_can_stream):
            first = wrapper.acquire(self.args)
            before = self.output.read_bytes()
            self.query.reset_mock()
            with patch.object(parquet_can_stream, "build_contract", side_effect=AssertionError("successful output must not rebuild")):
                second = wrapper.acquire(self.args)
        self.assertEqual(first["rows"], 6)
        self.assertEqual(second["rows"], 6)
        self.assertEqual(self.output.read_bytes(), before)
        self.query.assert_not_called()

    def test_early_os_error_does_not_expose_private_path(self):
        argv = ["parquet_can_signals", "--vin", self.args.vin, "--start", self.args.start, "--end", self.args.end,
                "--output", str(self.output), "--work-dir", str(self.work), "--run-id", RUN_ID]
        err = io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(wrapper, "acquire", side_effect=OSError(str(self.root))), redirect_stderr(err):
            self.assertEqual(wrapper.main(), 1)
        self.assertNotIn(str(self.root), err.getvalue())
        self.assertTrue(json.loads(err.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
