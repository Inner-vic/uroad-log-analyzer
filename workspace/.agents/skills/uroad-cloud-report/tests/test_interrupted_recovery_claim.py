"""Synthetic CAS takeover of an exact dead retained recovery attempt."""
from pathlib import Path
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_retained_recovery_claim as fixture_module
import scheduler
import task_registry as registry


class InterruptedRecoveryClaimTests(unittest.TestCase):
    def setUp(self):
        self.base = fixture_module.RetainedRecoveryClaimTests()
        self.base.setUp()
        self.dead_pid = 424242
        self.attempt_id = "b" * 32
        with registry.locked_registry() as payload:
            task = payload["tasks"][0]
            task.update(status="running", workerPid=self.dead_pid)
            task["execution"]["status"] = "running"
            task["retry"] = {"count": 1, "mode": "retained-can-recovery"}
            self.base.run["task"] = json.loads(json.dumps(task))
        self.base.run.update(status="running", error=None,
            recovery={"schema": "uroad-retained-can-recovery-v1", "attempts": [{"attemptId": self.attempt_id, "status": "running"}]})
        active = self.base.run["chunks"]["active"]
        active["status"] = "running"
        active["lanes"]["can"]["status"] = "completed"
        self.base.write_run()
        self.expected = {"slotName": ".heavy-slot-0.lock", "schema": scheduler.RETAINED_LEASE_SCHEMA,
                         "runId": self.base.job, "pid": self.dead_pid, "token": "c" * 32}
        self.slot = registry.runs_root() / self.expected["slotName"]
        self.slot.write_text(json.dumps({key: value for key, value in self.expected.items() if key != "slotName"},
                                       sort_keys=True, separators=(",", ":")), encoding="utf-8")
        self.dead = patch.object(registry, "_retained_pid_alive", return_value=False)
        self.workers = patch.object(registry, "_retained_workers_exited")
        self.dead.start(); self.workers.start()

    def tearDown(self):
        self.workers.stop(); self.dead.stop()
        self.base.tearDown()

    def acquire(self, **changes):
        return scheduler.acquire_interrupted_recovery_slot(self.base.job,
            expected_run_sha256=changes.get("sha", self.base.sha), context_binding=changes.get("binding", self.base.binding),
            expected_attempt_id=changes.get("attempt", self.attempt_id), expected_lease=changes.get("lease", self.expected))

    def rejected(self, **changes):
        old_registry, old_slot, old_run = registry.registry_path().read_bytes(), self.slot.read_bytes(), self.base.run_path.read_bytes()
        with self.assertRaises((RuntimeError, ValueError, OSError)):
            with self.acquire(**changes): self.fail("Unverified orphan accepted")
        self.assertEqual(registry.registry_path().read_bytes(), old_registry)
        self.assertEqual(self.slot.read_bytes(), old_slot)
        self.assertEqual(self.base.run_path.read_bytes(), old_run)

    def test_exact_dead_attempt_cas_stays_running_without_queue_or_run_mutation(self):
        original = self.base.run_path.read_bytes()
        with self.acquire() as task:
            self.assertEqual(task["status"], "running")
            self.assertEqual(task["workerPid"], os.getpid())
            self.assertEqual(task["retry"]["previousAttemptId"], self.attempt_id)
            self.assertEqual(task["retry"]["mode"], "interrupted-retained-can-recovery")
            self.assertEqual(task["retry"]["count"], 2)
            current = json.loads(self.slot.read_text())
            self.assertEqual(current["pid"], os.getpid())
            self.assertNotEqual(current["token"], self.expected["token"])
            self.assertEqual(scheduler.active_slot_run_ids(), [self.base.job])
            self.assertIsNone(registry.claim_next_queued_task())
            self.assertEqual(self.base.run_path.read_bytes(), original)
        self.assertFalse(self.slot.exists())

    def test_readonly_interrupted_preflight_preserves_every_file(self):
        before = [path.read_bytes() for path in (registry.registry_path(), self.slot, self.base.run_path)]
        task = registry.validate_interrupted_recovery(self.base.job, expected_run_sha256=self.base.sha,
            context_binding=self.base.binding, expected_attempt_id=self.attempt_id, expected_lease=self.expected)
        self.assertEqual(task["workerPid"], self.dead_pid)
        self.assertEqual([path.read_bytes() for path in (registry.registry_path(), self.slot, self.base.run_path)], before)

    def test_dead_merge_attempt_can_resume_without_faking_can_failure(self):
        active = self.base.run["chunks"]["active"]
        active.update(source="uroad+can", stage="merge")
        self.base.write_run()
        with self.acquire() as task:
            self.assertEqual(task["status"], "running")
            self.assertEqual(self.base.run["chunks"]["active"]["lanes"]["can"]["status"], "completed")

    def test_changed_sha_or_attempt_or_owner_or_lease_rejected(self):
        for changes in ({"sha": "0" * 64}, {"attempt": "a" * 32},
                        {"binding": {**self.base.binding, "messageId": "different"}},
                        {"lease": {**self.expected, "token": "d" * 32}}):
            with self.subTest(changes=changes): self.rejected(**changes)

    def test_live_parent_rejected(self):
        with patch.object(registry, "_retained_pid_alive", return_value=True): self.rejected()

    def test_live_owned_child_rejected(self):
        with patch.object(registry, "_retained_workers_exited", side_effect=registry.RegistryError("owned child exists")):
            self.rejected()

    def test_completed_final_artifact_rejects_recompute(self):
        (self.base.run_dir / "manifest.json").write_text("{}")
        self.rejected()

    def test_incomplete_can_or_wrong_attempt_status_rejected(self):
        original = json.loads(json.dumps(self.base.run))
        for field in ("can", "attempt"):
            self.base.run = json.loads(json.dumps(original))
            if field == "can": self.base.run["chunks"]["active"]["lanes"]["can"]["status"] = "running"
            else: self.base.run["recovery"]["attempts"][-1]["status"] = "completed"
            self.base.write_run()
            self.rejected()

    def test_registry_commit_failure_rolls_lease_back_and_preserves_registry_bytes(self):
        with patch.object(registry, "_write_registry", side_effect=OSError("synthetic commit failure")):
            self.rejected()

    def test_competing_original_token_cannot_steal_new_lease(self):
        with self.acquire():
            current = self.slot.read_bytes()
            with self.assertRaises(RuntimeError):
                with self.acquire(): self.fail("Old lease token reused")
            self.assertEqual(self.slot.read_bytes(), current)

    def test_exception_releases_new_lease_without_touching_original_run(self):
        original = self.base.run_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "synthetic body"):
            with self.acquire(): raise RuntimeError("synthetic body")
        self.assertFalse(self.slot.exists())
        self.assertEqual(self.base.run_path.read_bytes(), original)


if __name__ == "__main__": unittest.main()
