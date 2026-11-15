"""Prepare a fresh standalone workspace; no authorization, service or business job is started."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from check_repository import ROOT, verify


def prepare(workspace, base_python):
    if os.name != 'nt':
        raise ValueError('Windows target required')
    verify()
    if not workspace.is_absolute() or '..' in workspace.parts or workspace.exists():
        raise ValueError('Target must be an absent absolute workspace')
    workspace = workspace.absolute()
    if workspace.is_relative_to(ROOT) or ROOT.is_relative_to(workspace):
        raise ValueError('Keep repository and deployment separate')
    for path in (workspace,base_python):
        for item in (path,*path.parents):
            if item.exists():
                info=item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                    raise ValueError('Linked target or interpreter refused')
    subprocess.run([str(base_python),'-I','-c','import sys,struct;assert sys.implementation.name=="cpython";'
                    'assert sys.version_info[:2]==(3,13);assert struct.calcsize("P")==8'],check=True)
    shutil.copytree(ROOT/'workspace',workspace)
    env=workspace/'runtime/uroad-python'
    subprocess.run([str(base_python),'-I','-m','venv',str(env)],check=True)
    private=workspace/'private';private.mkdir()
    shutil.copyfile(ROOT/'examples/settings.json.example',private/'uroad.settings.json.example')
    shutil.copyfile(ROOT/'examples/environment.example.ps1',private/'environment.ps1.example')
    return {'status':'candidate_prepared','venv_created':True,'dependencies_installed':False,
            'services_registered':False,'authorization_started':False,'business_jobs_created':0}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--base-python',type=Path,required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(prepare(args.workspace,args.base_python)))
    except Exception as error:
        print(json.dumps({'status':'not_activated','error_type':type(error).__name__,
                          'instruction':'Retain any partial candidate for diagnosis; existing deployments are not overwritten.'}))
        sys.exit(1)
