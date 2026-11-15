import copy,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import task_registry as registry
import scheduler
import task_worker


class OwnedRecoveryHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'CC_CONNECT_WORKSPACE':str(self.root),'UROAD_MAX_HEAVY_JOBS':'2'});self.env.start()
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def task(self,name,status='claimed',owner=999999):
        record=registry.register_or_reuse(name,'DEM0VEH1CLEX00001','2026-11-02 20:57:00','2026-11-02 21:29:00','auto',
            metadata={'requester':{'id':'ou_owner'},'source':{'chatId':'oc_test','messageId':'om_'+name}})
        with registry.locked_registry() as payload:
            task=next(t for t in payload['tasks'] if t['runId']==name)
            task.update(status=status,startedAt='2026-01-01T00:00:00+00:00')
            task['execution']['status']=status
            task['claimantPid' if status=='claimed' else 'workerPid']=owner
            record=copy.deepcopy(task)
        return record
    def run_file(self,task,executed=False):
        root=registry.runs_root()/task['runId'];root.mkdir()
        run={'runId':task['runId'],'status':'running' if executed else 'queued','task':task,'request':task['request'],'artifacts':{},'steps':{}}
        path=root/'run.json';path.write_text(json.dumps(run),encoding='utf-8')
        if executed:(root/'slices').mkdir();(root/'slices/evidence.txt').write_text('do not overwrite')
        return path
    def lease(self,name,pid,slot=0):
        path=registry.runs_root()/f'.heavy-slot-{slot}.lock'
        path.write_text(json.dumps({'schema':scheduler.HEAVY_LEASE_SCHEMA,'runId':name,'pid':pid,'token':'a'*32}))
        return path
    def read_task(self,name):return next(t for t in json.loads(registry.registry_path().read_text(encoding='utf-8'))['tasks'] if t['runId']==name)

    def test_dead_unstarted_claim_requeues_both_statuses_while_other_slot_is_live(self):
        dead=self.task('run-dead');self.run_file(dead)
        self.lease('run-other',888888)
        with patch.object(registry,'_retained_pid_alive',side_effect=lambda pid:pid==888888):
            result=registry.recover_stale_tasks()
        self.assertEqual([r['runId'] for r in result],['run-dead'])
        task=self.read_task('run-dead');self.assertEqual((task['status'],task['execution']['status']),('queued','queued'))
        self.assertTrue((registry.runs_root()/'.heavy-slot-0.lock').exists())

    def test_live_claim_is_never_replayed_due_to_age(self):
        self.task('run-live')
        with patch.object(registry,'_retained_pid_alive',return_value=True):self.assertEqual(registry.recover_stale_tasks(),[])
        self.assertEqual(self.read_task('run-live')['status'],'claimed')

    def test_executed_orphan_fails_safely_and_archives_old_bytes_without_requeue(self):
        task=self.task('run-dead','running');path=self.run_file(task,True);before=path.read_bytes();lease=self.lease('run-dead',999999)
        with patch.object(registry,'_retained_pid_alive',return_value=False),patch.object(registry,'_retained_workers_exited'):
            result=registry.recover_stale_tasks()
        self.assertEqual(result[0]['status'],'system_error')
        run=json.loads(path.read_text(encoding='utf-8'));self.assertEqual(run['status'],'system_error');self.assertFalse(run['error']['retryable'])
        self.assertEqual((path.parent/run['failureRetention']['archive']).read_bytes(),before)
        self.assertEqual((path.parent/'slices/evidence.txt').read_text(),'do not overwrite')
        self.assertFalse(lease.exists());self.assertIsNone(registry.claim_next_queued_task())

    def test_live_owned_child_blocks_recovery_and_lease_removal(self):
        task=self.task('run-dead','running');path=self.run_file(task,True);before=path.read_bytes();lease=self.lease('run-dead',999999)
        with patch.object(registry,'_retained_pid_alive',return_value=False),patch.object(registry,'_retained_workers_exited',side_effect=registry.RegistryError('child alive')):
            self.assertEqual(registry.recover_stale_tasks(),[])
        self.assertEqual(path.read_bytes(),before);self.assertTrue(lease.exists())

    def test_registry_commit_failure_restores_original_run_and_keeps_lease(self):
        task=self.task('run-dead','running');path=self.run_file(task,True);before=path.read_bytes();lease=self.lease('run-dead',999999)
        original=registry._write_registry
        def fail(p,payload):
            if p.name==registry.REGISTRY_FILENAME:raise OSError('commit failed')
            return original(p,payload)
        with patch.object(registry,'_retained_pid_alive',return_value=False),patch.object(registry,'_retained_workers_exited'),patch.object(registry,'_write_registry',side_effect=fail):
            self.assertEqual(registry.recover_stale_tasks(),[])
        self.assertEqual(path.read_bytes(),before);self.assertTrue(lease.exists());self.assertEqual(self.read_task('run-dead')['status'],'running')

    def test_unknown_legacy_lease_is_never_deleted_or_guessed(self):
        task=self.task('run-dead','running');path=self.run_file(task,True);before=path.read_bytes()
        lease=registry.runs_root()/'.heavy-slot-0.lock';lease.write_text('run-dead')
        with patch.object(registry,'_retained_pid_alive',return_value=False):self.assertEqual(registry.recover_stale_tasks(),[])
        self.assertEqual(path.read_bytes(),before);self.assertEqual(lease.read_text(),'run-dead')

    def test_normal_slot_cleanup_preserves_replacement_owner(self):
        self.task('run-dead','queued',os.getpid())
        with scheduler.acquire_heavy_slot('run-dead'):
            lease=registry.runs_root()/'.heavy-slot-0.lock';data=json.loads(lease.read_text());self.assertEqual(data['pid'],os.getpid())
            data['token']='b'*32;lease.write_text(json.dumps(data))
        self.assertTrue(lease.exists())

    def test_same_job_cannot_acquire_two_execution_slots(self):
        self.task('run-dead','queued',os.getpid())
        with scheduler.acquire_heavy_slot('run-dead'):
            with self.assertRaises(registry.RegistryError):
                with scheduler.acquire_heavy_slot('run-dead'):self.fail('duplicate execution')
            self.assertEqual(len(list(registry.runs_root().glob('.heavy-slot-*.lock'))),1)
        self.assertFalse(list(registry.runs_root().glob('.heavy-slot-*.lock')))

    def test_worker_start_failure_closes_claim_without_rerunning_analysis(self):
        task=self.task('run-dead',owner=os.getpid());path=self.run_file(task)
        self.assertTrue(registry.mark_claim_start_failed(task['runId']))
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'],'system_error')
        self.assertEqual(self.read_task(task['runId'])['execution']['status'],'system_error')

    def test_claim_failure_commit_error_restores_initial_run(self):
        task=self.task('run-dead',owner=os.getpid());path=self.run_file(task);before=path.read_bytes()
        original=registry._write_registry
        def fail(p,data):
            if p.name==registry.REGISTRY_FILENAME:raise OSError('disk failure')
            return original(p,data)
        with patch.object(registry,'_write_registry',side_effect=fail),self.assertRaises(OSError):
            registry.mark_claim_start_failed(task['runId'])
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.read_task(task['runId'])['status'],'claimed')

    def test_failed_worker_child_does_not_strand_claim_or_stop_queue(self):
        registry.register_or_reuse('run-a','DEM0VEH1CLEX00001','s','e','auto')
        with patch.object(sys,'argv',['worker']),patch.object(task_worker,'run_one',side_effect=OSError('cannot start')):
            self.assertEqual(task_worker.main(),0)
        self.assertEqual(self.read_task('run-a')['status'],'system_error')


if __name__=='__main__':unittest.main()
