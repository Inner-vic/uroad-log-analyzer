import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

path=Path(__file__).resolve().parents[1]/'scripts/dispatch_uroad_worker.pyw'
loader=importlib.machinery.SourceFileLoader('uroad_native_launcher_test',str(path))
spec=importlib.util.spec_from_loader(loader.name,loader)
native=importlib.util.module_from_spec(spec);loader.exec_module(native)


class NativeDispatcherLauncherTests(unittest.TestCase):
    def test_real_hidden_worker_and_finite_exit_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);worker=root/'bounded_worker.py';marker=root/'done'
            worker.write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("done")\n',encoding='utf-8')
            state=root/'state.json'
            self.assertEqual(native.launch(sys.executable,worker,root,state),0)
            self.assertEqual(marker.read_text(),'done')
            receipt=json.loads(state.read_text(encoding='utf-8'))
            self.assertEqual((receipt['stage'],receipt['exitCode']),('exited',0))
            self.assertGreater(receipt['workerPid'],0)

    def test_other_interpreter_or_cross_workspace_worker_cannot_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);worker=root/'worker.py';worker.write_text('')
            folder=root/'separate';folder.mkdir()
            with patch.object(native.subprocess,'Popen',side_effect=AssertionError('must not start')),self.assertRaises(ValueError):
                native.launch(sys.executable,worker,folder,folder/'state.json')


if __name__=='__main__':unittest.main()
