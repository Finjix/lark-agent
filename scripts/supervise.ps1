$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$logDirectory=Join-Path $projectRoot 'data\logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
while ($true) {
    $stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
    $serviceProcess=Start-Process -FilePath (Join-Path $projectRoot 'runtime\node\node.exe') -ArgumentList @(('"'+(Join-Path $projectRoot 'dist\main.js')+'"')) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory ($stamp+'.log')) -RedirectStandardError (Join-Path $logDirectory ($stamp+'.error.log')) -Wait -PassThru
    if ($serviceProcess.ExitCode -eq 0) { break }
    Start-Sleep -Seconds 10
}
