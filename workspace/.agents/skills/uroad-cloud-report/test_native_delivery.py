import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import delivery_bridge as delivery


class NativeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.report = self.root / "report.html"
        self.report.write_text("<!doctype html><html>verified report</html>", encoding="utf-8")
        self.run_path = self.root / "run.json"
        self.payload = {"runId": "run-bound", "artifacts": {"report": str(self.report)}, "task": {
            "context": {"chatId": "oc_source", "messageId": "om_source", "requesterId": "ou_owner", "threadId": ""},
            "source": {"chatId": "oc_source", "messageId": "om_source"},
            "requester": {"id": "ou_owner"}, "routing": {"threadId": ""}}}
        self.run_path.write_text(json.dumps(self.payload), encoding="utf-8")
        self.status = "pending"
        self.marks = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(delivery, "validate_attachment_path", return_value={
            "report": str(self.report), "filename": self.report.name, "sizeBytes": self.report.stat().st_size}).start()
        mock.patch.object(delivery, "_native_binary", return_value=Path("cc-connect-context.exe")).start()
        mock.patch.object(delivery, "delivery_status", side_effect=lambda _: self.status).start()
        def mark(run_id, status, **fields):
            self.status = status
            self.marks.append((run_id, status, fields))
        mock.patch.object(delivery, "mark_delivery", side_effect=mark).start()

    def completion(self, stdout=" Message sent successfully.\n"):
        return subprocess.CompletedProcess([], 0, stdout, "")

    def test_pure_ok_is_not_delivery_and_platform_message_id_is_compatible(self):
        self.assertFalse(delivery.delivery_succeeded({"ok": True}))
        self.assertFalse(delivery.delivery_succeeded({"success": True, "status": "sent"}))
        self.assertTrue(delivery.delivery_succeeded({"ok": True, "message_id": "om_platform"}))
        self.assertFalse(delivery.delivery_succeeded({"ok": True, "messageId": {"id": "invented"}}))
        audit = delivery.record_delivery_result("run-bound", {"message_id": "om_platform", "receipt": {"platform": "feishu"}})
        self.assertEqual(audit["status"], "sent")

    def test_native_ack_has_bound_durable_receipt_and_retry_does_not_resend(self):
        with mock.patch.object(delivery.subprocess, "run", return_value=self.completion()) as send:
            self.assertEqual(delivery.send_native_report(self.run_path)["status"], "sent")
            receipt = json.loads((self.root / "standard-report-delivery-receipt.json").read_text())
            self.assertEqual(receipt["source"], self.payload["task"]["context"])
            self.assertEqual(receipt["payloadSha256"], delivery._hash_file(self.report))
            self.assertEqual(receipt["exitCode"], 0)
            self.assertNotIn("messageId", receipt)
            self.assertEqual(delivery.send_native_report(self.run_path)["status"], "already_sent")
            self.assertEqual(send.call_count, 1)
        # A crash between durable receipt and registry commit is repaired locally.
        self.status = "pending"
        with mock.patch.object(delivery.subprocess, "run") as send:
            self.assertEqual(delivery.send_native_report(self.run_path)["status"], "sent")
            send.assert_not_called()

    def test_exit_zero_without_ack_is_not_sent(self):
        with mock.patch.object(delivery.subprocess, "run", return_value=self.completion("{\"ok\":true}\n")):
            self.assertEqual(delivery.send_native_report(self.run_path)["status"], "failed")
        self.assertEqual(self.status, "delivery_failed")
        self.assertFalse((self.root / "standard-report-delivery-receipt.json").exists())

    def test_wrong_source_or_hash_receipt_prevents_send(self):
        for field, bad in (("source", {"chatId": "oc_other", "messageId": "om_source", "requesterId": "ou_owner", "threadId": ""}),
                           ("payloadSha256", "0" * 64), ("runId", "run-other")):
            with self.subTest(field=field):
                receipt = delivery._native_receipt(self.completion(), "run-bound", delivery._session_key(self.payload),
                                                   delivery._source_binding(self.payload), report=self.report)
                receipt[field] = bad
                (self.root / "standard-report-delivery-receipt.json").write_text(json.dumps(receipt))
                self.status = "pending"
                with mock.patch.object(delivery.subprocess, "run") as send:
                    self.assertEqual(delivery.send_native_report(self.run_path)["status"], "failed")
                    send.assert_not_called()

    def test_conflicting_context_source_prevents_send(self):
        self.payload["task"]["source"]["chatId"] = "oc_other"
        self.run_path.write_text(json.dumps(self.payload))
        with mock.patch.object(delivery.subprocess, "run") as send:
            self.assertEqual(delivery.send_native_report(self.run_path)["status"], "failed")
            send.assert_not_called()

    def test_concurrent_sender_returns_in_progress_without_sending_or_marking(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def send(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test sender did not release")
            return self.completion()
        with mock.patch.object(delivery.subprocess, "run", side_effect=send) as native:
            first = threading.Thread(target=lambda: results.append(delivery.send_native_report(self.run_path)))
            first.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(delivery.send_native_report(self.run_path)["status"], "in_progress")
                self.assertEqual(self.marks, [])
            finally:
                release.set()
                first.join(5)
            self.assertFalse(first.is_alive())
            self.assertEqual(results[0]["status"], "sent")
            self.assertEqual(native.call_count, 1)
            self.assertEqual(len(self.marks), 1)


if __name__ == "__main__":
    unittest.main()
