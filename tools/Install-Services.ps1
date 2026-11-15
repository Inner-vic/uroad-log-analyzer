param(
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$PythonExecutable
)
$ErrorActionPreference='Stop'
$root=(Resolve-Path -LiteralPath $Workspace).Path
$python=(Resolve-Path -LiteralPath $PythonExecutable).Path
$pythonw=Join-Path (Split-Path -LiteralPath $python) 'pythonw.exe'
$scripts=Join-Path $root '.agents/skills/uroad-cloud-report/scripts'
$launcher=Join-Path $scripts 'dispatch_uroad_worker.pyw'
$cleanup=Join-Path $scripts 'retention_cleanup.py'
$dispatchName='CC Connect Uroad Dispatcher'
$retentionName='CC Connect Uroad Retention Cleanup'
$metadataPath=Join-Path $root 'deployment/uroad-dispatcher.installation.json'
foreach($target in @($root,$python,$pythonw,$launcher,$cleanup)) {
    $item=Get-Item -LiteralPath $target
    while($null -ne $item) {
        if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Linked deployment path refused'}
        if($item -is [IO.DirectoryInfo]){$item=$item.Parent}else{$item=$item.Directory}
    }
}
if([IO.Path]::GetFileName($python) -ne 'python.exe'){throw 'Controlled python.exe required'}
if(Test-Path -LiteralPath $metadataPath){throw 'Existing deployment metadata will not be overwritten'}
foreach($name in @($dispatchName,$retentionName)) {
    if(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue){throw 'Existing task must be reviewed; no overwrite'}
}
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$arguments='-B "'+$launcher+'" --python-executable "'+$python+'"'
$action=New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $root
$trigger=New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $dispatchName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
$cleanupAction=New-ScheduledTaskAction -Execute $python -Argument ('-B "'+$cleanup+'" --workspace "'+$root+'"') -WorkingDirectory $root
$cleanupTrigger=New-ScheduledTaskTrigger -Daily -At '03:30'
Register-ScheduledTask -TaskName $retentionName -Action $cleanupAction -Trigger $cleanupTrigger -Principal $principal -Settings $settings | Out-Null
$deployment=Split-Path -LiteralPath $metadataPath
New-Item -ItemType Directory -Path $deployment -Force | Out-Null
@{
    schema='uroad-dispatcher-installation-v1'; taskName=$dispatchName; workspace=$root;
    python=$python; actionExecutable=$pythonw; actionArguments=$arguments;
    dispatcherSha256=(Get-FileHash -LiteralPath $launcher -Algorithm SHA256).Hash.ToLowerInvariant();
    installedAt=[DateTime]::UtcNow.ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8
Write-Output 'Services registered. First dispatcher trigger is scheduled; validate configuration before invoking this installer.'
