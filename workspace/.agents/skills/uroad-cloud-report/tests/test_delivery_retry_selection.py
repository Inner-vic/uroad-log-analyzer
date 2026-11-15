"""Completed-report retries include the registry's actual failure status."""
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import delivery_bridge
import task_registry


class DeliveryRetrySelectionTests(unittest.TestCase):
    def task(self, run_id="run-synthetic", execution="completed", delivery="pending", report=True):
        return {"runId": run_id, "status": execution, "execution": {"status": execution},
                "delivery": {"status": delivery},
                "artifacts": {"report": "synthetic-report.html"} if report else {}}

    @contextmanager
    def registry(self, tasks):
        @contextmanager
        def locked():
            yield {"tasks": tasks}
        with patch.object(task_registry, "locked_registry", locked):
            yield

    def test_actual_failed_delivery_is_selected_for_existing_report_retry(self):
        task = self.task()
        with self.registry([task]):
            result = delivery_bridge.record_delivery_result(task["runId"], {"ok": False, "error": "synthetic failure"})
            self.assertEqual(result["status"], "failed")
            self.assertEqual(task["delivery"]["status"], "delivery_failed")
            self.assertEqual([item["runId"] for item in delivery_bridge.retry_candidates()], [task["runId"]])

    def test_pending_and_legacy_failed_remain_eligible_without_requeueing_analysis(self):
        tasks = [self.task("run-pending"), self.task("run-legacy", delivery="failed"),
                 self.task("run-normalized", delivery="delivery_failed")]
        with self.registry(tasks):
            result = task_registry.pending_deliveries()
        self.assertEqual([task["runId"] for task in result], [task["runId"] for task in tasks])
        self.assertTrue(all(task["execution"]["status"] == "completed" for task in tasks))

    def test_sent_uploading_unfinished_and_missing_report_are_excluded(self):
        tasks = [self.task("run-sent", delivery="sent"), self.task("run-uploading", delivery="uploading"),
                 self.task("run-running", execution="running", delivery="delivery_failed"),
                 self.task("run-failed-execution", execution="system_error", delivery="delivery_failed"),
                 self.task("run-missing-report", delivery="delivery_failed", report=False)]
        with self.registry(tasks):
            self.assertEqual(task_registry.pending_deliveries(), [])


if __name__ == "__main__":
    unittest.main()
