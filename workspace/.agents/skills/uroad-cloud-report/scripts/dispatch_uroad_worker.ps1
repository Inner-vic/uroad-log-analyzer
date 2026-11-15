param([Parameter(Mandatory=$true)][string]$PythonExecutable)
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../../..'))
$worker = Join-Path $PSScriptRoot 'task_worker.py'
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) { throw 'Controlled interpreter unavailable' }
if ([IO.Path]::GetFileName($PythonExecutable) -ne 'python.exe') { throw 'Controlled interpreter must be python.exe' }
if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) { throw 'Worker unavailable' }
$env:CC_CONNECT_WORKSPACE = $workspace
$env:UROAD_TRIGGER_WORKER = '0'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$child = Start-Process -FilePath $PythonExecutable -ArgumentList @(('"' + $worker + '"'),'--loop','--idle-seconds','90') -WorkingDirectory $workspace -WindowStyle Hidden -Wait -PassThru
exit $child.ExitCode
