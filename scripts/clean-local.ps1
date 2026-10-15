[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param()

$ErrorActionPreference = 'Stop'
$projectPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $projectPath 'src\main.ts'))) {
    throw 'Project root could not be verified.'
}

# Keep the runtime, installed dependencies, dist, credentials, production DB,
# logs, saved Feishu review draft, and existing audio output.
$relativePaths = @(
    'releases',
    'data\离线定稿验收 空格',
    'data\离线复验 空格',
    'data\离线验收 空格',
    'data\最终离线验收 空格',
    'data\screenshots',
    'data\delivery-verification.json',
    'data\feishu-card-verification.json',
    'data\offline-verification.json',
    'data\interrupt-27c3327b.json',
    'data\interrupt-27c3327b.sqlite',
    'data\verify-54602c08.json',
    'data\verify-54602c08.sqlite',
    'data\verify-622a25cf.json',
    'data\verify-622a25cf.sqlite',
    'data\verify-94403044.json',
    'data\verify-94403044.sqlite',
    'runtime\npm-cache',
    'runtime\node-v24.21.0-win-x64.zip',
    'output\desktop-research'
)

# Validate all targets before deleting any of them.
$targets = foreach ($relativePath in $relativePaths) {
    $absolutePath = [IO.Path]::GetFullPath((Join-Path $projectPath $relativePath))
    if (-not $absolutePath.StartsWith($projectPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Target outside project: $absolutePath"
    }
    if (-not (Test-Path -LiteralPath $absolutePath)) { continue }
    $item = Get-Item -LiteralPath $absolutePath -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing reparse point: $absolutePath"
    }
    if ($item.PSIsContainer) {
        $links = @(Get-ChildItem -LiteralPath $absolutePath -Recurse -Force -Attributes ReparsePoint)
        if ($links.Count -gt 0) { throw "Refusing directory containing reparse points: $absolutePath" }
    }
    $item
}

$removedBytes = 0L
foreach ($item in $targets) {
    if ($PSCmdlet.ShouldProcess($item.FullName, 'Delete generated files and caches')) {
        $bytes = if ($item.PSIsContainer) {
            (Get-ChildItem -LiteralPath $item.FullName -Recurse -File -Force | Measure-Object Length -Sum).Sum
        } else { $item.Length }
        Remove-Item -LiteralPath $item.FullName -Recurse -Force
        $removedBytes += $bytes
    }
}
Write-Output ('Freed {0:N2} MB' -f ($removedBytes / 1MB))
