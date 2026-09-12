import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, '/workspace/skills/uroad-cloud-report/scripts')

import task_registry
import task_worker
import run_uroad_task


class TaskWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        os.environ['OPENCLAW_WORKSPACE'] = str(self.workspace)
        (self.workspace / 'outputs' / 'uroad-report-runs').mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop('OPENCLAW_WORKSPACE', None)
        self.tempdir.cleanup()

    def _register(self, run_id: str, *, cloud_env: str = 'auto'):
        task_registry.register_or_reuse(run_id, 'DEM0VEH1CLE000001', f'2026-09-04 11:2{run_id[-1]}:00', f'2026-09-04 11:2{run_id[-1]}:59', cloud_env)

    def _load_registry(self):
        return json.loads(task_registry.registry_path().read_text(encoding='utf-8'))

    def test_claim_next_queued_task_marks_claimed(self):
        self._register('run-a')
        claimed = task_registry.claim_next_queued_task()
        self.assertEqual(claimed['runId'], 'run-a')
        payload = self._load_registry()
        self.assertEqual(payload['tasks'][0]['status'], 'claimed')
        self.assertIsNotNone(payload['tasks'][0]['startedAt'])

    def test_mark_dequeued_resets_started_at(self):
        self._register('run-a')
        task_registry.claim_next_queued_task()
        task_registry.mark_dequeued('run-a')
        payload = self._load_registry()
        self.assertEqual(payload['tasks'][0]['status'], 'queued')
        self.assertIsNone(payload['tasks'][0]['startedAt'])

    def test_register_or_reuse_treats_claimed_as_duplicate(self):
        self._register('run-a')
        task_registry.claim_next_queued_task()
        with self.assertRaises(task_registry.DuplicateTaskError) as ctx:
            task_registry.register_or_reuse('run-b', 'DEM0VEH1CLE000001', '2026-09-04 11:2a:00'.replace('a', 'a'), '2026-09-04 11:2a:59'.replace('a', 'a'), 'auto')
        self.assertFalse(ctx.exception.reusable)

    def test_worker_run_once_requeues_on_busy_exit_code(self):
        self._register('run-a')
        completed = mock.Mock(returncode=3)
        with mock.patch('task_worker.subprocess.run', return_value=completed):
            code = task_worker.run_once()
        self.assertEqual(code, 3)
        payload = self._load_registry()
        self.assertEqual(payload['tasks'][0]['status'], 'queued')

    def test_worker_loop_drains_multiple_tasks(self):
        self._register('run-a')
        task_registry.register_or_reuse('run-b', 'DEM0VEH1CLE000001', '2026-09-04 11:29:00', '2026-09-04 11:29:59', 'prod')

        def fake_run(*args, **kwargs):
            payload = self._load_registry()
            for task in payload['tasks']:
                if task['status'] == 'claimed':
                    task_registry.mark_finished(task['runId'], 'completed')
                    break
            return mock.Mock(returncode=0)

        with mock.patch.object(sys, 'argv', ['task_worker.py', '--loop', '--poll-seconds', '0.01']):
            with mock.patch('task_worker.subprocess.run', side_effect=fake_run):
                exit_code = task_worker.main()
        self.assertEqual(exit_code, 0)
        payload = self._load_registry()
        self.assertEqual([task['status'] for task in payload['tasks']], ['completed', 'completed'])

    def test_no_data_is_debounced_then_requeryable(self):
        task_registry.register_or_reuse('run-a', 'DEM0VEH1CLE000001', '2026-09-04 11:20:00', '2026-09-04 11:20:59', 'auto')
        task_registry.mark_finished('run-a', 'no_data')
        with self.assertRaises(task_registry.DuplicateTaskError) as ctx:
            task_registry.register_or_reuse('run-b', 'DEM0VEH1CLE000001', '2026-09-04 11:20:00', '2026-09-04 11:20:59', 'auto')
        self.assertFalse(ctx.exception.reusable)
        payload = self._load_registry()
        payload['tasks'][0]['completedAt'] = '2020-01-01T00:00:00+00:00'
        task_registry.registry_path().write_text(json.dumps(payload), encoding='utf-8')
        fresh = task_registry.register_or_reuse('run-b', 'DEM0VEH1CLE000001', '2026-09-04 11:20:00', '2026-09-04 11:20:59', 'auto')
        self.assertEqual(fresh['status'], 'queued')

    def test_only_completed_report_is_reusable(self):
        task_registry.register_or_reuse('run-a', 'DEM0VEH1CLE000001', '2026-09-04 11:20:00', '2026-09-04 11:20:59', 'auto')
        task_registry.mark_finished('run-a', 'completed', report=str(self.workspace / 'missing.html'))
        fresh = task_registry.register_or_reuse('run-b', 'DEM0VEH1CLE000001', '2026-09-04 11:20:00', '2026-09-04 11:20:59', 'auto')
        self.assertEqual(fresh['status'], 'queued')
        task_registry.mark_finished('run-b', 'completed', report=str(self.workspace / 'report.html'))
        report = self.workspace / 'report.html'
        report.write_text('ok', encoding='utf-8')
        payload = self._load_registry()
        payload['tasks'][1]['artifacts']['report'] = str(report)
        task_registry.registry_path().write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaises(task_registry.DuplicateTaskError) as ctx:
            task_registry.register_or_reuse('run-c', 'DEM0VEH1CLE000001', '2026-09-04 11:20:00', '2026-09-04 11:20:59', 'auto')
        self.assertTrue(ctx.exception.reusable)

    def test_state_chain_claimed_running_completed(self):
        self._register('run-a')
        claimed = task_registry.claim_next_queued_task()
        self.assertEqual(claimed['status'], 'claimed')
        running = task_registry.mark_running('run-a')
        self.assertEqual(running['status'], 'running')
        task_registry.mark_finished('run-a', 'completed')
        payload = self._load_registry()
        task = payload['tasks'][0]
        self.assertEqual(task['status'], 'completed')
        self.assertIsNotNone(task['startedAt'])
        self.assertIsNotNone(task['completedAt'])

    def test_run_uroad_task_spawns_loop_worker(self):
        popen_calls = []

        def fake_popen(command, **kwargs):
            popen_calls.append(command)
            return mock.Mock()

        with mock.patch.object(sys, 'argv', ['run_uroad_task.py', '--vin', 'DEM0VEH1CLE000001', '--start', '2026-09-04 11:20:00', '--end', '2026-09-04 11:20:59', '--defer-to-worker']):
            with mock.patch('run_uroad_task.subprocess.run', return_value=mock.Mock(returncode=0)):
                with mock.patch('run_uroad_task.subprocess.Popen', side_effect=fake_popen):
                    exit_code = run_uroad_task.main()
        self.assertEqual(exit_code, 0)
        self.assertTrue(popen_calls)
        self.assertIn('--loop', popen_calls[0])


if __name__ == '__main__':
    unittest.main()
