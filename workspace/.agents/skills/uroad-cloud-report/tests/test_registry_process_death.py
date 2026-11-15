import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import task_registry as registry


class RegistryProcessDeathTests(unittest.TestCase):
    def test_killed_lock_owner_does_not_block_next_request(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'CC_CONNECT_WORKSPACE':tmp}):
            root=Path(tmp)
            child=root/'hold.py'
            child.write_text('import sys,time\nsys.path.insert(0,'+repr(str(Path(registry.__file__).parent))+')\nimport task_registry as r\nwith r.locked_registry():\n print("held",flush=True)\n time.sleep(30)\n',encoding='utf-8')
            process=subprocess.Popen([sys.executable,'-B',str(child)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env={**os.environ,'PYTHONUTF8':'1'})
            try:
                self.assertEqual(process.stdout.readline().strip(),'held')
                with self.assertRaises(registry.RegistryError):
                    with registry.locked_registry(timeout_seconds=.1):self.fail('concurrent writer')
                process.kill();process.wait(timeout=5)
                with registry.locked_registry(timeout_seconds=1) as payload:payload['deathRecoveryVerified']=True
                self.assertTrue(json.loads(registry.registry_path().read_text(encoding='utf-8'))['deathRecoveryVerified'])
                self.assertTrue(registry.registry_lock_path().is_file())
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=5)
                process.stdout.close();process.stderr.close()

    def test_lock_inode_is_stable_across_transactions(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'CC_CONNECT_WORKSPACE':tmp}):
            with registry.locked_registry() as payload:payload['n']=1
            before=registry.registry_lock_path().stat().st_ino
            with registry.locked_registry() as payload:payload['n']+=1
            self.assertEqual(registry.registry_lock_path().stat().st_ino,before)
            self.assertEqual(json.loads(registry.registry_path().read_text(encoding='utf-8'))['n'],2)


if __name__=='__main__':unittest.main()
