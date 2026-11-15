from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))


class ConcurrencyAndFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ["CC_CONNECT_WORKSPACE"] = self.temp.name
        os.environ["UROAD_MAX_HEAVY_JOBS"] = "2"
        os.environ["UROAD_RETENTION_DAYS"] = "14"
        from task_registry import register_or_reuse
        self.register = register_or_reuse
        self.a = self.register("run-20260916-010101-000001-aaaaaaaa", "DEM0VEH1CLEX00001", "2026-09-14 19:15:00", "2026-09-14 19:15:59", "auto", metadata={"requester": {"id": "alice"}, "source": {"chatId": "chat-a", "messageId": "m-a"}, "routing": {"threadId": "thread-a"}})
        self.b = self.register("run-20260916-010102-000001-bbbbbbbb", "DEM0VEH1CLEX00001", "2026-09-14 20:15:00", "2026-09-14 20:15:59", "auto", metadata={"requester": {"id": "bob"}, "source": {"chatId": "chat-b", "messageId": "m-b"}, "routing": {"threadId": "thread-b"}})

    def tearDown(self):
        self.temp.cleanup()

    def _run_dir(self, run_id):
        path = Path(self.temp.name) / "outputs" / "uroad-report-runs" / run_id
        path.mkdir(parents=True)
        (path / "analysis").mkdir()
        (path / "report").mkdir()
        (path / "run.json").write_text(json.dumps({"runId": run_id, "status": "completed", "completedAt": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        return path

    def test_two_slots_and_fair_claims(self):
        from scheduler import acquire_heavy_slot, available_heavy_slots
        from task_registry import claim_next_queued_task
        first = claim_next_queued_task()
        second = claim_next_queued_task()
        self.assertEqual(first["runId"], self.a["runId"])
        self.assertEqual(second["runId"], self.b["runId"])
        with acquire_heavy_slot(first["runId"]):
            self.assertEqual(available_heavy_slots(), 1)
            with acquire_heavy_slot(second["runId"]):
                self.assertEqual(available_heavy_slots(), 0)

    def test_followup_is_thread_and_owner_scoped(self):
        from followup_context import enqueue_followup, register_data_context
        run_dir = self._run_dir(self.a["runId"])
        context = register_data_context(self.a["runId"], run_dir)
        self.assertEqual(context["version"], 1)
        entry = enqueue_followup(self.a["runId"], question="active=1 时 idl 是否为 1？", requester_id="alice", chat_id="chat-a", thread_id="thread-a", message_id="q-a")
        self.assertEqual(entry["status"], "running")
        queued = enqueue_followup(self.a["runId"], question="继续检查档位", requester_id="alice", chat_id="chat-a", thread_id="thread-a", message_id="q-a-2")
        self.assertEqual(queued["status"], "queued")
        from followup_context import complete_followup
        completed = complete_followup(self.a["runId"], entry["questionId"], summary="已完成")
        self.assertEqual(completed["status"], "completed")
        with self.assertRaises(RuntimeError):
            enqueue_followup(self.a["runId"], question="读取别人的任务", requester_id="bob", chat_id="chat-b", thread_id="thread-b", message_id="q-b")

    def test_first_message_question_is_enqueued_after_report_data(self):
        from followup_context import register_data_context
        task = self.register(
            "run-20260916-010103-000001-cccccccc",
            "DEM0VEH1CLEX00001",
            "2026-09-14 21:15:00",
            "2026-09-14 21:15:59",
            "auto",
            metadata={"requester": {"id": "alice"}, "source": {"chatId": "chat-a", "messageId": "m-c"}, "routing": {"threadId": "thread-a"}},
            attached_questions=["请分析 active=1 时的其他状态", "请补充事件前后变化"],
        )
        context = register_data_context(task["runId"], self._run_dir(task["runId"]))
        self.assertEqual(context["version"], 1)
        from task_registry import locked_registry
        with locked_registry() as payload:
            saved = next(item for item in payload["tasks"] if item["runId"] == task["runId"])
        entries = saved["followups"]
        self.assertEqual([entry["source"] for entry in entries], ["initial_message", "initial_message"])
        self.assertEqual([entry["status"] for entry in entries], ["running", "queued"])
        self.assertEqual(entries[0]["question"], "请分析 active=1 时的其他状态")
    def test_retention_removes_only_expired_finished_runs(self):
        from retention_cleanup import apply_retention
        run_dir = self._run_dir(self.a["runId"])
        run_json = run_dir / "run.json"
        payload = json.loads(run_json.read_text(encoding="utf-8"))
        payload["completedAt"] = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        run_json.write_text(json.dumps(payload), encoding="utf-8")
        result = apply_retention(Path(self.temp.name))
        self.assertIn(self.a["runId"], result["removedRuns"])
        self.assertFalse(run_dir.exists())


if __name__ == "__main__":
    unittest.main()