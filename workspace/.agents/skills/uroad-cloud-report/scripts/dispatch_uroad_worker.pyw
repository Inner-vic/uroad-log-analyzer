"""Windowless task-service launcher; worker uses the controlled python.exe."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


def launch(python, worker, workspace, state_path):
    python,worker,workspace=Path(python).resolve(strict=True),Path(worker).resolve(strict=True),Path(workspace).resolve(strict=True)
    if python.name.lower()!='python.exe' or not worker.is_relative_to(workspace):
        raise ValueError('Invalid controlled worker binding')
    def state(**fields):
        value={'schema':'uroad-dispatcher-state-v1','launcherPid':os.getpid(),**fields}
        temporary=state_path.with_name(state_path.name+'.'+uuid4().hex+'.tmp')
        try:
            with temporary.open('x',encoding='utf-8') as stream:
                json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
            temporary.replace(state_path)
        finally:temporary.unlink(missing_ok=True)
    env={**os.environ,'CC_CONNECT_WORKSPACE':str(workspace),'UROAD_TRIGGER_WORKER':'0','PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    options={'cwd':workspace,'env':env,'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL,'close_fds':True}
    if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW
    began=datetime.now(timezone.utc).isoformat()
    child=subprocess.Popen([str(python),'-B',str(worker),'--loop','--idle-seconds','90'],**options)
    state(stage='running',workerPid=child.pid,startedAt=began)
    code=child.wait()
    state(stage='exited',workerPid=child.pid,startedAt=began,endedAt=datetime.now(timezone.utc).isoformat(),exitCode=code)
    return code


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--python-executable',required=True,type=Path)
    args=parser.parse_args()
    scripts=Path(__file__).resolve().parent
    os.environ['CC_CONNECT_WORKSPACE']=str(scripts.parents[3])
    if args.python_executable.resolve()!=Path(sys.executable).with_name('python.exe').resolve():
        raise ValueError('Interpreter does not match the windowless launcher')
    sys.path.insert(0,str(scripts))
    from task_registry import runs_root,workspace_root
    return launch(args.python_executable,scripts/'task_worker.py',workspace_root(),runs_root()/'.dispatcher-state.json')


if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        # No raw command, credentials or task contents in the task-service UI.
        sys.exit(1)
