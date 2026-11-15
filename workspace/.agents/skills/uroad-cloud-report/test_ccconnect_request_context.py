import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
from ccconnect_request_context import RequestContextError, followup_identity_from_context, merge_transport_metadata, metadata_from_context_header


THREADED = '[cc-connect request_context_v=1 sender_id=ou_owner platform=feishu chat_id=oc_team message_id=om_followup thread_id=om_report]'


class CCConnectRequestContextTests(unittest.TestCase):
    def test_threaded_context_becomes_canonical_metadata(self):
        metadata = metadata_from_context_header(THREADED)
        self.assertEqual(metadata["requester"], {"id": "ou_owner"})
        self.assertEqual(metadata["source"], {"chatId": "oc_team", "messageId": "om_followup"})
        self.assertEqual(metadata["routing"], {"threadId": "om_report"})

    def test_context_rejects_missing_transport_fields(self):
        with self.assertRaises(RequestContextError):
            metadata_from_context_header('[cc-connect request_context_v=1 sender_id=ou_owner platform=feishu chat_id=oc_team message_id= thread_id=om_report]')

    def test_transport_identity_cannot_be_overridden(self):
        context = metadata_from_context_header(THREADED)
        with self.assertRaises(RequestContextError):
            merge_transport_metadata(context, {"raw": {"chatId": "oc_other"}})

    def test_followup_identity_uses_same_thread_root(self):
        self.assertEqual(
            followup_identity_from_context(THREADED),
            {"requester_id": "ou_owner", "chat_id": "oc_team", "thread_id": "om_report", "message_id": "om_followup"},
        )


if __name__ == "__main__":
    unittest.main()