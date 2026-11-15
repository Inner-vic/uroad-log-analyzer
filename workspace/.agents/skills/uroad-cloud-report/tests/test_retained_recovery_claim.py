from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scheduler
import task_registry as registry


class RetainedRecoveryClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"CC_CONNECT_WORKSPACE": self.temp.name,
            "UROAD_MAX_HEAVY_JOBS": "2", "UROAD_MIN_FREE_GB": "1"})
        self.environment.start()
        self.disk = patch.object(scheduler.shutil, "disk_usage", return_value=type("Disk", (), {"free": 10**12})())
        self.disk.start()
        self.job = "run-20261103-110153-634967-e655ceef"
        self.binding = {"requesterId": "owner", "chatId": "chat", "messageId": "original-message", "threadId": ""}
        task = registry.register_or_reuse(self.job, "DEM0VEH1CLE000001", "2026-11-03 10:30:00",
            "2026-11-03 10:35:00", "auto", metadata={"raw": dict(self.binding)})
        snapshot = registry.mark_running(self.job)
        snapshot.pop("workerPid")
        registry.mark_finished(self.job, "system_error", error="Synthetic transfer failure")
        with registry.locked_registry() as payload:
            payload["tasks"][0].pop("workerPid")
        self.run = {"runId": self.job, "status": "system_error", "task": snapshot,
            "request": copy.deepcopy(task["request"]),
            "input": {"vin": task["request"]["vin"], "start": task["request"]["start"],
                      "end": task["request"]["end"], "cloud_env": "auto",
                      "resolved_cloud_env": "test", "resolved_provider_environment": "testtwo"},
            "environmentSelection": {"mode": "auto", "selected": {"environment": "test", "providerEnvironment": "testtwo"}},
            "error": {"retryable": True, "message": "Synthetic transfer failure", "source": "can", "stage": "parquet_can", "slice": 1},
            "chunks": {"active": {"index": 1, "start": task["request"]["start"], "end": task["request"]["end"],
                "source": "can", "stage": "parquet_can", "status": "failed", "branchesDrained": True,
                "environment": "test", "lanes": {"uroad": {"status": "completed"}, "can": {"status": "failed", "stage": "parquet_can"}}}}}
        self.run_dir = registry.runs_root() / self.job
        self.run_dir.mkdir()
        self.run_path = self.run_dir / "run.json"
        self.write_run()

    def tearDown(self):
        self.disk.stop()
        self.environment.stop()
        self.temp.cleanup()

    def write_run(self):
        self.run_path.write_text(json.dumps(self.run, ensure_ascii=False, indent=2), encoding="utf-8")
        self.sha = hashlib.sha256(self.run_path.read_bytes()).hexdigest()

    def acquire(self, **kwargs):
        return scheduler.acquire_retained_recovery_slot(self.job,
            expected_run_sha256=kwargs.get("sha", self.sha), context_binding=kwargs.get("binding", self.binding))

    def leases(self):
        return list(registry.runs_root().glob(".heavy-slot-*.lock"))

    def assert_rejected_unchanged(self, **kwargs):
        original = registry.registry_path().read_bytes()
        original_run = self.run_path.read_bytes()
        with self.assertRaises((registry.RegistryError, RuntimeError, ValueError)):
            with self.acquire(**kwargs):
                self.fail("Unverified retained task was claimed")
        self.assertEqual(registry.registry_path().read_bytes(), original)
        self.assertEqual(self.run_path.read_bytes(), original_run)
        self.assertEqual(self.leases(), [])

    def test_success_running_directly_preserves_run_and_original_source(self):
        original = self.run_path.read_bytes()
        with self.acquire() as task:
            self.assertEqual(task["status"], "running")
            self.assertEqual(task["execution"]["status"], "running")
            self.assertEqual(task["context"]["messageId"], "original-message")
            self.assertEqual(task["request"], self.run["request"])
            self.assertEqual(task["retry"]["mode"], "retained-can-recovery")
            self.assertEqual(task["retry"]["expectedRunSha256"], self.sha)
            self.assertEqual(task["retry"]["count"], 1)
            self.assertEqual(task["workerPid"], os.getpid())
            self.assertEqual(scheduler.active_slot_run_ids(), [self.job])
            self.assertIsNone(registry.claim_next_queued_task())
            self.assertEqual(self.run_path.read_bytes(), original)
        self.assertEqual(self.leases(), [])

    def test_identity_binding_conflicts_never_rewrite_registry(self):
        for field in self.binding:
            with self.subTest(field=field):
                wrong = {**self.binding, field: "different"}
                self.assert_rejected_unchanged(binding=wrong)

    def test_thread_none_is_same_original_main_thread(self):
        with self.acquire(binding={**self.binding, "threadId": None}) as task:
            self.assertEqual(task["status"], "running")

    def test_alias_conflict_rejected_even_if_canonical_context_matches(self):
        self.run["task"]["source"]["chatId"] = "other-chat"
        self.write_run()
        self.assert_rejected_unchanged()

    def test_embedded_request_and_input_conflict_rejected(self):
        self.run["input"]["vin"] = "DEM0VEH1CLE000002"
        self.write_run()
        self.assert_rejected_unchanged()

    def test_top_level_source_alias_conflict_rejected(self):
        self.run["source"] = {"messageId": "another-source"}
        self.write_run()
        self.assert_rejected_unchanged()

    def test_wrong_sha_rejected(self):
        self.assert_rejected_unchanged(sha="0" * 64)

    def test_run_changes_between_precheck_and_locked_claim_rejected(self):
        locked = registry.locked_registry
        @contextmanager
        def changed_before_lock():
            self.run_path.write_bytes(self.run_path.read_bytes() + b"\n")
            with locked() as payload:
                yield payload
        initial_registry = registry.registry_path().read_bytes()
        with patch.object(registry, "locked_registry", changed_before_lock):
            with self.assertRaises(registry.RegistryError):
                with self.acquire():
                    self.fail("Changed run was claimed")
        self.assertEqual(registry.registry_path().read_bytes(), initial_registry)
        self.assertEqual(self.leases(), [])

    def test_run_changes_during_locked_validation_rejected_before_commit(self):
        validate = registry._retained_checked_validate
        calls = 0
        def changed_after_validate(*args):
            nonlocal calls
            validate(*args)
            calls += 1
            if calls == 2:
                self.run_path.write_bytes(self.run_path.read_bytes() + b"\n")
        original = registry.registry_path().read_bytes()
        with patch.object(registry, "_retained_checked_validate", changed_after_validate):
            with self.assertRaises(registry.RegistryError):
                with self.acquire(): self.fail("Late changed run accepted")
        self.assertEqual(original, registry.registry_path().read_bytes())
        self.assertEqual(self.leases(), [])

    def test_terminal_and_drain_guards(self):
        original = copy.deepcopy(self.run)
        changes = (("status", "running"), ("retryable", False), ("drained", False),
                   ("drained", 1), ("uroad", "failed"), ("canstage", "can_stream"), ("environment", "prod"))
        for field, value in changes:
            with self.subTest(field=field, value=value):
                self.run = copy.deepcopy(original)
                if field == "status": self.run["status"] = value
                elif field == "retryable": self.run["error"]["retryable"] = value
                elif field == "drained": self.run["chunks"]["active"]["branchesDrained"] = value
                elif field == "uroad": self.run["chunks"]["active"]["lanes"]["uroad"]["status"] = value
                elif field == "canstage": self.run["chunks"]["active"]["lanes"]["can"]["stage"] = value
                else: self.run["chunks"]["active"]["environment"] = value
                self.write_run()
                self.assert_rejected_unchanged()

    def test_registry_queued_cannot_be_taken_by_recovery(self):
        with registry.locked_registry() as payload:
            payload["tasks"][0]["status"] = "queued"
        self.assert_rejected_unchanged()

    def test_registry_duplicate_exact_run_rejected(self):
        with registry.locked_registry() as payload:
            payload["tasks"].append(copy.deepcopy(payload["tasks"][0]))
        self.assert_rejected_unchanged()

    def test_recovery_count_is_bounded_and_boolean_is_not_integer(self):
        for count in (True, -1, 16):
            with self.subTest(count=count):
                with registry.locked_registry() as payload:
                    payload["tasks"][0]["retry"] = {"count": count}
                self.assert_rejected_unchanged()

    def test_malformed_nested_fields_fail_as_registry_error(self):
        self.run["environmentSelection"]["selected"]["environment"] = []
        self.write_run()
        with self.assertRaises(registry.RegistryError):
            with self.acquire(): self.fail("Malformed selection accepted")
        self.assertEqual(self.leases(), [])

    def test_resolved_environment_input_conflict_rejected(self):
        self.run["input"]["resolved_provider_environment"] = "different-provider"
        self.write_run()
        self.assert_rejected_unchanged()

    def test_delivery_and_validated_report_guards(self):
        original = copy.deepcopy(self.run)
        for field, value in (("delivery", {"status": "sent"}), ("reportValidation", {"status": "passed"}),
                             ("artifacts", {"report": "synthetic-report.html"})):
            with self.subTest(field=field):
                self.run = copy.deepcopy(original)
                self.run[field] = value
                self.write_run()
                self.assert_rejected_unchanged()

    def test_claim_without_owned_lease_rejected_without_write(self):
        before = registry.registry_path().read_bytes()
        with self.assertRaises(RuntimeError):
            registry.claim_retained_recovery(self.job, self.sha, self.binding)
        self.assertEqual(before, registry.registry_path().read_bytes())

    def test_existing_exact_run_lease_prevents_duplicate_claim(self):
        other = registry.runs_root() / ".heavy-slot-0.lock"
        other.write_text(self.job, encoding="utf-8")
        before = registry.registry_path().read_bytes()
        with self.assertRaises(RuntimeError):
            with self.acquire(): self.fail("Duplicate lease accepted")
        self.assertEqual(before, registry.registry_path().read_bytes())
        self.assertEqual(self.leases(), [other])
        self.assertEqual(other.read_text(), self.job)

    def test_owned_lease_wrong_pid_is_rejected(self):
        lease = registry.runs_root() / ".heavy-slot-0.lock"
        lease.write_text(json.dumps({"schema": scheduler.RETAINED_LEASE_SCHEMA, "runId": self.job,
                                   "pid": os.getpid() + 1, "token": "a" * 32}), encoding="utf-8")
        before = registry.registry_path().read_bytes()
        with self.assertRaises(RuntimeError):
            registry.claim_retained_recovery(self.job, self.sha, self.binding)
        self.assertEqual(before, registry.registry_path().read_bytes())

    def test_same_task_competitor_cannot_take_running_recovery(self):
        errors = []
        with self.acquire() as task:
            def attempt():
                try:
                    with self.acquire(): errors.append("unexpected-success")
                except (registry.RegistryError, RuntimeError): errors.append("rejected")
            contender = threading.Thread(target=attempt)
            contender.start(); contender.join(timeout=5)
            self.assertFalse(contender.is_alive())
            self.assertEqual(errors, ["rejected"])
            self.assertEqual(len(self.leases()), 1)
            self.assertEqual(task["status"], "running")

    def test_exception_in_caller_releases_only_owned_lease(self):
        other = registry.runs_root() / ".heavy-slot-1.lock"
        other.write_text("another-run", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "synthetic caller"):
            with self.acquire(): raise RuntimeError("synthetic caller")
        self.assertEqual(self.leases(), [other])

    def test_replaced_lease_is_not_deleted_by_finally(self):
        with self.acquire():
            lease = self.leases()[0]
            lease.unlink()
            lease.write_text("different-owner", encoding="utf-8")
        self.assertEqual(lease.read_text(), "different-owner")

    def test_slot_write_failure_releases_partial_own_lease(self):
        with patch.object(scheduler.os, "write", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                with self.acquire(): self.fail("Write failure ignored")
        self.assertEqual(self.leases(), [])

    def test_live_old_worker_blocks_recovery(self):
        with registry.locked_registry() as payload:
            payload["tasks"][0]["workerPid"] = os.getpid()
        self.run["task"]["workerPid"] = os.getpid()
        self.write_run()
        self.assert_rejected_unchanged()

    def test_old_mark_running_still_rejects_system_error(self):
        with self.assertRaises(registry.RegistryError): registry.mark_running(self.job)

    def test_low_disk_never_claims_task(self):
        with patch.object(scheduler.shutil, "disk_usage", return_value=type("Disk", (), {"free": 0})()):
            self.assert_rejected_unchanged()


if __name__ == "__main__": unittest.main()
