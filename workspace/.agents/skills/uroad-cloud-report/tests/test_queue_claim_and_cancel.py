"""Exact queued metadata, options and honest cancellation, all synthetic."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import task_registry as registry
import task_worker as worker
import run_uroad_pipeline as pipeline


class QueueClaimAndCancelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"CC_CONNECT_WORKSPACE": self.temp.name})
        self.environment.start()
        self.job = "run-20261103-123456-123456-1234abcd"
        self.options = {"eeaPlatform": "EEA3.0", "pageSize": 41, "maxFiles": 2}
        self.task = registry.register_or_reuse(self.job, "DEM0VEH1CLE000001", "2026-11-03 10:30:00", "2026-11-03 10:35:00", "test",
            metadata={"requester": {"id": "owner"}, "source": {"chatId": "chat", "messageId": "original"}, "routing": {"threadId": None}},
            execution_options=self.options)
        self.identity = registry.request_identity("DEM0VEH1CLE000001", "2026-11-03 10:30:00", "2026-11-03 10:35:00", "test")

    def tearDown(self):
        self.environment.stop(); self.temp.cleanup()

    def metadata(self, task=None):
        os.environ[registry.REQUEST_METADATA_ENV] = json.dumps(worker.task_metadata(task or self.task))
        return registry.load_request_metadata()

    def test_worker_transport_preserves_full_task_record_and_nondefault_options(self):
        with patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as called:
            self.assertEqual(worker.run_one(self.task), 0)
        command = called.call_args.args[0]
        self.assertEqual(command[command.index("--page-size") + 1], "41")
        self.assertEqual(command[command.index("--max-files") + 1], "2")
        metadata = json.loads(command[command.index("--request-metadata") + 1])
        self.assertEqual(metadata["taskRecord"]["request"], self.task["request"])
        self.assertEqual(called.call_args.kwargs["env"]["UROAD_TRIGGER_WORKER"], "0")

    def test_metadata_round_trip_and_registry_claim_preserve_request(self):
        claimed = registry.claim_next_queued_task()
        metadata = self.metadata(claimed)
        self.assertEqual(metadata["taskRecord"], claimed)
        actual = registry.existing_task_for_execution(self.job, self.identity, metadata, execution_options=self.options)
        self.assertEqual(actual["request"], self.task["request"])
        self.assertEqual(actual["context"], self.task["context"])

    def test_forged_copy_wrong_context_request_or_options_rejected_without_registry_change(self):
        original = registry.registry_path().read_bytes()
        for mutation in (lambda m: m["taskRecord"]["request"].update(vin="DEM0VEH1CLE000002"),
                         lambda m: m["source"].update(messageId="different")):
            metadata = self.metadata()
            mutation(metadata)
            with self.assertRaises(registry.RegistryError):
                registry.existing_task_for_execution(self.job, self.identity, metadata, execution_options=self.options)
            self.assertEqual(registry.registry_path().read_bytes(), original)
        with self.assertRaises(registry.RegistryError):
            registry.existing_task_for_execution(self.job, self.identity, self.metadata(), execution_options={**self.options, "maxFiles": None})
        self.assertEqual(registry.registry_path().read_bytes(), original)

    def test_legacy_missing_options_default_but_explicit_invalid_options_reject(self):
        self.assertEqual(registry.execution_options_for_task({"request": {}}), {"eeaPlatform": "EEA3.0", "pageSize": 100, "maxFiles": None})
        for value in (None, {}, {**self.options, "pageSize": True}, {**self.options, "maxFiles": 0}, {**self.options, "extra": 1}):
            with self.subTest(value=value):
                with self.assertRaises(registry.RegistryError):
                    registry.execution_options_for_task({"request": {"executionOptions": value}})

    def test_owner_can_cancel_queued_task_with_empty_admin_allowlist(self):
        result = registry.request_cancel(self.job, requester_id="owner", allowed_requesters=set())
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(registry.claim_next_queued_task())

    def test_foreign_or_missing_requester_cannot_cancel(self):
        original = registry.registry_path().read_bytes()
        for requester in ("different", None):
            with self.assertRaises(registry.RegistryError): registry.request_cancel(self.job, requester_id=requester)
            self.assertEqual(registry.registry_path().read_bytes(), original)

    def test_running_cancel_reports_unsupported_without_false_success_or_marker(self):
        registry.mark_running(self.job)
        original = registry.registry_path().read_bytes()
        with self.assertRaisesRegex(registry.RegistryError, "尚未停止"):
            registry.request_cancel(self.job, requester_id="owner", allowed_requesters=set())
        self.assertEqual(registry.registry_path().read_bytes(), original)
        self.assertNotIn("cancelRequestedAt", json.loads(original)["tasks"][0])

    def test_real_queue_metadata_enters_pipeline_and_failure_keeps_full_original_request(self):
        import cheyun_prod_client as cloud
        import process_time_chunks
        import retention_cleanup
        import scheduler
        claimed = registry.claim_next_queued_task()
        self.metadata(claimed)
        run_dir = registry.runs_root() / self.job
        run_dir.mkdir()
        (run_dir / "run.json").write_text('{"status":"queued","error":null}')
        argv = ["pipeline", "--existing-run-id", self.job, "--vin", self.identity.vin, "--start", self.identity.start,
                "--end", self.identity.end, "--cloud-env", "test", "--page-size", "41", "--max-files", "2"]
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(patch.object(pipeline, "validate_workspace", return_value=Path(self.temp.name)))
            stack.enter_context(patch.object(pipeline, "bootstrap_dependencies"))
            stack.enter_context(patch.object(cloud, "load_settings", return_value={"username": "synthetic"}))
            stack.enter_context(patch.object(cloud, "query_files", return_value=type("Listing", (), {"items": [{}], "total": 1, "download_hosts": []})()))
            stack.enter_context(patch.object(cloud, "provider_environment", return_value="testtwo"))
            stack.enter_context(patch.object(retention_cleanup, "apply_retention", return_value={"ok": True}))
            stack.enter_context(patch.object(scheduler.shutil, "disk_usage", return_value=type("Disk", (), {"free": 10**12})()))
            stack.enter_context(patch.object(process_time_chunks, "process", side_effect=RuntimeError("synthetic stopped before acquisition")))
            stack.enter_context(redirect_stdout(io.StringIO())); stack.enter_context(redirect_stderr(io.StringIO()))
            self.assertEqual(pipeline.main(), 1)
        saved = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "system_error")
        self.assertEqual(saved["request"], self.task["request"])
        self.assertEqual(saved["task"]["request"], self.task["request"])
        self.assertEqual(saved["input"]["page_size"], 41)
        self.assertEqual(saved["input"]["max_files"], 2)


if __name__ == "__main__": unittest.main()
