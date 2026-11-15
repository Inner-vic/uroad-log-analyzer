import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import task_registry as registry
import run_uroad_pipeline as pipeline
import run_uroad_task as entry
from process_time_chunks import EvidenceStore


class DurableAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'CC_CONNECT_WORKSPACE':str(self.root),'UROAD_TRIGGER_WORKER':'0',
            'UROAD_REQUEST_METADATA':json.dumps({'requester':{'id':'ou_owner'},'source':{'chatId':'oc_chat','messageId':'om_request'},'routing':{'threadId':'om_root'}})})
        self.env.start()
        self.args=['--vin','DEM0VEH1CLEX00001','--start','2026-11-02 20:57:00','--end','2026-11-02 21:29:00']
    def tearDown(self):
        self.env.stop();self.tmp.cleanup()

    def test_real_admission_persists_before_claim_without_any_cloud_or_parse(self):
        output=io.StringIO()
        with patch.object(sys,'argv',['pipeline',*self.args,'--enqueue-only','--page-size','7','--max-files','3']), \
             patch.object(pipeline,'bootstrap_dependencies'), \
             patch('cheyun_prod_client.load_settings',return_value={'username':'u'}), \
             patch('cheyun_prod_client.query_files',side_effect=AssertionError('no query during admission')), \
             patch('process_time_chunks.process',side_effect=AssertionError('no parse during admission')), \
             patch('retention_cleanup.apply_retention',side_effect=AssertionError('no cleanup on business path')), \
             contextlib.redirect_stdout(output):
            self.assertEqual(pipeline.main(),0)
        result=json.loads(output.getvalue());self.assertTrue(result['queued'])
        record=json.loads(Path(result['record_path']).read_text(encoding='utf-8'))
        claimed=registry.claim_next_queued_task()
        self.assertEqual(claimed['runId'],result['runId'])
        self.assertEqual(record['task']['context'],claimed['context'])
        self.assertEqual(record['request'],claimed['request'])
        self.assertEqual(claimed['request']['executionOptions'],{'pageSize':7,'maxFiles':3,'eeaPlatform':'EEA3.0'})
        self.assertEqual(record['housekeeping']['mode'],'scheduled_only')

    def test_prepare_failure_never_publishes_claimable_task(self):
        def fail(task): raise OSError('simulated disk write failure')
        with self.assertRaises(OSError):
            registry.register_or_reuse('run-test','DEM0VEH1CLEX00001','s','e','auto',prepare_callback=fail)
        self.assertIsNone(registry.claim_next_queued_task())

    def test_new_entry_always_requests_enqueue_only(self):
        completed=Mock(returncode=0,stdout='{"ok":true,"queued":true,"runId":"run-test"}',stderr='')
        output=io.StringIO()
        with patch.object(sys,'argv',['entry',*self.args]),patch.object(entry.subprocess,'run',return_value=completed) as run,contextlib.redirect_stdout(output):
            self.assertEqual(entry.main(),0)
        self.assertIn('--enqueue-only',run.call_args.args[0])
        self.assertEqual(json.loads(output.getvalue())['runId'],'run-test')

    def test_worker_child_remains_foreground_for_its_owned_claim(self):
        completed=Mock(returncode=0,stdout='{"ok":true}',stderr='')
        with patch.object(sys,'argv',['entry',*self.args,'--existing-run-id','run-test']),patch.object(entry.subprocess,'run',return_value=completed) as run,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(entry.main(),0)
        self.assertNotIn('--enqueue-only',run.call_args.args[0])

    def test_failed_worker_launch_is_reported_without_losing_queued_job(self):
        completed=Mock(returncode=0,stdout='{"ok":true,"queued":true,"runId":"run-test"}',stderr='')
        output=io.StringIO()
        with patch.dict(os.environ,{'UROAD_TRIGGER_WORKER':'1'}),patch.object(sys,'argv',['entry',*self.args]), \
             patch.object(entry.subprocess,'run',return_value=completed),patch.object(entry,'start_background_worker',side_effect=OSError('failure')),contextlib.redirect_stdout(output):
            self.assertEqual(entry.main(),1)
        result=json.loads(output.getvalue());self.assertTrue(result['queued']);self.assertFalse(result['ok']);self.assertFalse(result['worker']['started'])

    def test_detached_worker_survives_launcher_exit(self):
        marker=self.root/'worker-finished'
        worker=self.root/'bounded_worker.py'
        worker.write_text('import time\nfrom pathlib import Path\ntime.sleep(.3)\nPath('+repr(str(marker))+').write_text("done")\n',encoding='utf-8')
        launcher=self.root/'launcher.py'
        launcher.write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(Path(entry.__file__).parent))+')\nimport run_uroad_task as e\ne.TASK_WORKER=Path('+repr(str(worker))+')\nprint(e.start_background_worker())\n',encoding='utf-8')
        done=subprocess.run([sys.executable,'-B',str(launcher)],capture_output=True,text=True,timeout=8)
        self.assertEqual(done.returncode,0,done.stderr)
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(marker.read_text(),'done')

    def dispatcher_manifest(self):
        directory=self.root/'deployment';directory.mkdir()
        config={'schema':'uroad-dispatcher-installation-v1','taskName':'CC Connect Uroad Dispatcher','workspace':str(self.root),'python':sys.executable,
            'dispatcherSha256':hashlib.sha256((Path(entry.__file__).parent/'dispatch_uroad_worker.pyw').read_bytes()).hexdigest(),
            'actionExecutable':'powershell.exe','actionArguments':'approved-dispatcher-only'}
        path=directory/'uroad-dispatcher.installation.json';path.write_text(json.dumps(config),encoding='utf-8')
        return path,config

    @unittest.skipUnless(os.name=='nt','Windows task service')
    def test_installed_dispatcher_is_used_instead_of_request_child_process(self):
        self.dispatcher_manifest()
        with patch.object(entry.subprocess,'run',return_value=Mock(returncode=0)) as command,patch.object(entry.subprocess,'Popen',side_effect=AssertionError('must use service')):
            result=entry.start_background_worker()
        self.assertEqual(result,{'started':True,'method':'scheduled_task'})
        self.assertIn('Start-ScheduledTask',command.call_args.args[0][-1])
        self.assertIn('$t.Actions[0].Execute',command.call_args.args[0][-1])

    @unittest.skipUnless(os.name=='nt','Windows task service')
    def test_tampered_dispatcher_cannot_fall_back_to_unverified_execution(self):
        path,config=self.dispatcher_manifest();config['dispatcherSha256']='0'*64;path.write_text(json.dumps(config),encoding='utf-8')
        with patch.object(entry.subprocess,'run',side_effect=AssertionError('do not call service')),self.assertRaises(RuntimeError):
            entry.start_background_worker()

    @unittest.skipUnless(os.name=='nt','Windows task service')
    def test_service_start_failure_does_not_launch_another_worker(self):
        self.dispatcher_manifest()
        with patch.object(entry.subprocess,'run',return_value=Mock(returncode=1)),patch.object(entry.subprocess,'Popen',side_effect=AssertionError('no uncontrolled fallback')),self.assertRaises(RuntimeError):
            entry.start_background_worker()

    @unittest.skipUnless(os.name=='nt','Windows task service')
    def test_missing_production_dispatcher_does_not_silently_use_request_lifetime(self):
        scripts=self.root/'.agents/skills/uroad-cloud-report/scripts';scripts.mkdir(parents=True)
        with patch.object(entry,'SCRIPT_ROOT',scripts),patch.object(entry.subprocess,'Popen',side_effect=AssertionError('no weak fallback')),self.assertRaises(RuntimeError):
            entry.start_background_worker()

    def test_disk_merge_preserves_input_and_all_evidence_without_cloning_records(self):
        original={'events':[{'eventId':'e1','ts':'2026-11-02T20:57:00','detail':{'汉字':[1,2]}}],
                  'frames':[], 'charts':{'fpsSeries':[{'ts':'2026-11-02T20:57:00','value':10}], 'canSignals':{'old':'value'}},
                  'summary':{'totalFrames':0}}
        before=json.dumps(original,ensure_ascii=False,sort_keys=True)
        store=EvidenceStore(self.root/'merge.sqlite3')
        insert=store._insert
        seen=[]
        def observe(kind,row):seen.append(row);insert(kind,row)
        try:
            with patch.object(store,'_insert',side_effect=observe):store.add_slice(1,original,{'signals':{}})
            self.assertIs(seen[0],original['events'][0])
            self.assertEqual(store.rows('events'),original['events'])
            self.assertEqual(store.rows('series:fpsSeries'),original['charts']['fpsSeries'])
            self.assertEqual(json.dumps(original,ensure_ascii=False,sort_keys=True),before)
            contract=next(store.iter_contracts());self.assertNotIn('events',contract);self.assertNotIn('canSignals',contract['charts'])
        finally:store.close()


if __name__=='__main__':unittest.main()
