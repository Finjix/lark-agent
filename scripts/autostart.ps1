param([switch]$Remove)
$ErrorActionPreference='Stop'
$projectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$startupFolder=[Environment]::GetFolderPath('Startup')
$shortcutPath=Join-Path $startupFolder 'LarkDesktopAgent.lnk'
if ($Remove) { if(Test-Path -LiteralPath $shortcutPath){Remove-Item -LiteralPath $shortcutPath}; Write-Output 'Autostart removed'; exit }
$shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
$shortcut.TargetPath=(Get-Command powershell.exe).Source
$shortcut.Arguments='-NoProfile -WindowStyle Hidden -File "'+(Join-Path $projectRoot 'scripts\supervise.ps1')+'"'
$shortcut.WorkingDirectory=$projectRoot
$shortcut.WindowStyle=7
$shortcut.Save()
Write-Output 'Autostart installed for current user. Codex Desktop must also be started and logged in.'
