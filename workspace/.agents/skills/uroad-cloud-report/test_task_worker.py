import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import task_registry
import task_worker
import run_uroad_task


class TaskWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        os.environ["CC_CONNECT_WORKSPACE"] = str(self.workspace)
        os.environ["UROAD_MAX_HEAVY_JOBS"] = "2"

    def tearDown(self):
        os.environ.pop("CC_CONNECT_WORKSPACE", None)
        os.environ.pop("UROAD_MAX_HEAVY_JOBS", None)
        self.tempdir.cleanup()

    def _register(self, run_id: str, *, requester="alice", message_id=None, cloud_env="auto"):
        metadata = {"requester": {"id": requester}, "source": {"chatId": f"chat-{requester}", "messageId": message_id or run_id}, "routing": {"threadId": f"thread-{requester}"}}
        return task_registry.register_or_reuse(run_id, "DEM0VEH1CLEX00003", f"2026-09-04 11:2{run_id[-1]}:00", f"2026-09-04 11:2{run_id[-1]}:59", cloud_env, metadata=metadata)

    def _load_registry(self):
        return json.loads(task_registry.registry_path().read_text(encoding="utf-8"))

    def test_claim_next_queued_task_marks_claimed(self):
        self._register("run-a")
        claimed = task_registry.claim_next_queued_task()
        self.assertEqual(claimed["runId"], "run-a")
        self.assertEqual(self._load_registry()["tasks"][0]["status"], "claimed")

    def test_same_message_is_idempotent_but_same_request_new_message_is_allowed(self):
        self._register("run-a", message_id="m-1")
        with self.assertRaises(task_registry.DuplicateTaskError) as duplicate:
            self._register("run-b", message_id="m-1")
        self.assertTrue(duplicate.exception.reusable)
        fresh = self._register("run-c", message_id="m-2")
        self.assertEqual(fresh["status"], "queued")

    def test_worker_loop_drains_queued_task(self):
        self._register("run-a")

        def finish(task):
            task_registry.mark_running(task["runId"])
            task_registry.mark_finished(task["runId"], "completed")
            return 0

        with mock.patch.object(sys, "argv", ["task_worker.py", "--loop", "--poll-seconds", "0.01", "--idle-seconds", "0.02"]):
            with mock.patch("task_worker.run_one", side_effect=finish):
                self.assertEqual(task_worker.main(), 0)
        self.assertEqual(self._load_registry()["tasks"][0]["status"], "completed")

    def test_state_chain_claimed_running_completed(self):
        self._register("run-a")
        task_registry.claim_next_queued_task()
        self.assertEqual(task_registry.mark_running("run-a")["status"], "running")
        task_registry.mark_finished("run-a", "completed")
        self.assertEqual(self._load_registry()["tasks"][0]["status"], "completed")

    def test_retryable_system_error_can_be_requeued_in_place(self):
        task = self._register("run-a")
        task_registry.mark_running(task["runId"])
        task_registry.mark_finished(task["runId"], "system_error", error="transient parser failure")
        run_dir = task_registry.runs_root() / task["runId"]
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"runId": task["runId"], "error": {"message": "transient parser failure", "retryable": True}}),
            encoding="utf-8",
        )

        retried = task_registry.requeue_retryable(task["runId"])

        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["execution"]["status"], "queued")
        self.assertEqual(retried["retry"]["count"], 1)
        self.assertEqual(retried["context"]["messageId"], task["context"]["messageId"])

    def test_nonretryable_system_error_stays_terminal(self):
        task = self._register("run-a")
        task_registry.mark_running(task["runId"])
        task_registry.mark_finished(task["runId"], "system_error", error="permanent failure")
        run_dir = task_registry.runs_root() / task["runId"]
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"runId": task["runId"], "error": {"message": "permanent failure", "retryable": False}}),
            encoding="utf-8",
        )

        with self.assertRaises(task_registry.RegistryError):
            task_registry.requeue_retryable(task["runId"])
        self.assertEqual(self._load_registry()["tasks"][0]["status"], "system_error")

    def test_run_uroad_task_spawns_worker_for_queued_result(self):
        popen_calls = []
        completed = mock.Mock(returncode=0, stdout=json.dumps({"queued": True}), stderr="")
        with mock.patch.object(sys, "argv", ["run_uroad_task.py", "--vin", "DEM0VEH1CLEX00003", "--start", "2026-09-04 11:20:00", "--end", "2026-09-04 11:20:59"]):
            with mock.patch("run_uroad_task.subprocess.run", return_value=completed) as run_mock, \
                    mock.patch("run_uroad_task._scheduled_worker", return_value=None), \
                    mock.patch("run_uroad_task.should_trigger_worker", return_value=True):
                with mock.patch("run_uroad_task.subprocess.Popen", side_effect=lambda command, **kwargs: (popen_calls.append(command) or mock.Mock(pid=12345))):
                    self.assertEqual(run_uroad_task.main(), 0)
        run_kwargs = run_mock.call_args.kwargs
        self.assertEqual(run_kwargs["encoding"], "utf-8")
        self.assertEqual(run_kwargs["errors"], "replace")
        self.assertEqual(run_kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(run_kwargs["env"]["PYTHONUTF8"], "1")
        self.assertTrue(popen_calls)
        self.assertIn("--loop", popen_calls[0])


if __name__ == "__main__":
    unittest.main()
