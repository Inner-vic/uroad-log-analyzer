"""Progress is a read, not a follow-up claim or another vehicle task."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import task_status as status
from task_registry import RegistryError

HEADER='[cc-connect request_context_v=1 sender_id=ou_owner platform=feishu chat_id=oc_chat message_id=om_question thread_id=om_thread]'
RUN='run-20261103-000000-000001-aaaaaaaa'


class TaskStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'registry.json'
        self.task={'runId':RUN,'status':'running','context':{'requesterId':'ou_owner','chatId':'oc_chat','messageId':'om_source','threadId':'om_thread','accessScope':'owner'},
            'execution':{'status':'running'},'delivery':{'status':'pending'},'private':'do not expose'}
        self.save()
        self.patch=patch.object(status,'registry_path',return_value=self.path);self.patch.start();self.addCleanup(self.patch.stop)

    def save(self,extra=None):self.path.write_text(json.dumps({'tasks':[self.task,*(extra or [])]}),encoding='utf-8')

    def test_running_read_does_not_rewrite_registry_or_enqueue_question(self):
        before=self.path.stat().st_mtime_ns
        value=status.status(HEADER)
        self.assertEqual(value['status'],'running');self.assertFalse(value['delivered'])
        self.assertEqual(self.path.stat().st_mtime_ns,before);self.assertNotIn('private',value)

    def test_foreign_owner_and_chat_cannot_read_status(self):
        for header in [HEADER.replace('ou_owner','ou_other'),HEADER.replace('oc_chat','oc_other')]:
            with self.assertRaises(RegistryError):status.status(header,RUN)

    def test_ambiguous_thread_and_missing_binding_do_not_guess_recent(self):
        other={**self.task,'runId':RUN+'b'};self.save([other])
        with self.assertRaises(RegistryError):status.status(HEADER)
        with self.assertRaises(RegistryError):status.status(HEADER.replace('thread_id=om_thread','thread_id='))
        self.assertEqual(status.status(HEADER,RUN)['jobId'],RUN)

    def test_complete_without_receipt_is_not_report_delivery(self):
        self.task['execution']['status']='completed';self.task['delivery']['status']='sent';self.save()
        value=status.status(HEADER,RUN)
        self.assertFalse(value['delivered']);self.assertIn('交付确认',value['message'])

    def test_native_receipt_must_match_report_source_and_run(self):
        import delivery_bridge as bridge
        source={key:self.task['context'][key] for key in ('requesterId','chatId','messageId','threadId')}
        receipt={'schema':bridge.NATIVE_RECEIPT_SCHEMA,'acknowledgement':bridge.NATIVE_ACK,'exitCode':0,
            'runId':RUN,'source':source,'sessionKey':bridge._session_for_source(source),
            'payloadKind':'report','payloadSha256':'a'*64,'sizeBytes':100}
        self.task['execution']['status']='completed'
        self.task['delivery']={'status':'sent','receipt':receipt};self.save()
        self.assertTrue(status.status(HEADER,RUN)['delivered'])
        for field,value in [('runId',RUN+'b'),('payloadKind','text')]:
            self.task['delivery']['receipt']={**receipt,field:value};self.save()
            self.assertFalse(status.status(HEADER,RUN)['delivered'])
        foreign={**source,'messageId':'om_other'}
        self.task['delivery']['receipt']={**receipt,'source':foreign,'sessionKey':bridge._session_for_source(foreign)};self.save()
        self.assertFalse(status.status(HEADER,RUN)['delivered'])

    def test_source_message_is_not_outbound_delivery_evidence(self):
        self.task['execution']['status']='completed'
        self.task['delivery']={'status':'sent','messageId':'om_source'};self.save()
        self.assertFalse(status.status(HEADER,RUN)['delivered'])
        self.task['delivery']['messageId']='om_actual_reply';self.save()
        self.assertTrue(status.status(HEADER,RUN)['delivered'])

    def test_delivery_failed_does_not_propose_another_analysis(self):
        self.task['execution']['status']='completed';self.task['delivery']['status']='delivery_failed';self.save()
        value=status.status(HEADER,RUN)
        self.assertIn('单独恢复交付',value['message']);self.assertFalse(value['delivered'])


if __name__=='__main__':unittest.main()
