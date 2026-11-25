param(
    [Parameter(Mandatory = $true)]
    [string]$BuildPython
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = (Resolve-Path -LiteralPath $BuildPython).Path
$staging = Join-Path $root ('artifacts\windows_candidate_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
if (Test-Path -LiteralPath $staging) {
    throw "Build output already exists: $staging"
}
& $python -c 'import platform, struct, PyInstaller; assert platform.system() == "Windows" and struct.calcsize("P") == 8'
if ($LASTEXITCODE -ne 0) {
    throw 'Use a reviewed Windows x64 build environment with PyInstaller installed.'
}
& $python (Join-Path $PSScriptRoot 'windows_preflight.py') (Join-Path $staging 'environment.json')
if ($LASTEXITCODE -ne 0) { throw 'Build environment differs from the project locks.' }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Build environment has broken dependencies.' }

$arguments = @(
    '-m', 'PyInstaller',
    '--distpath', (Join-Path $staging 'dist'),
    '--workpath', (Join-Path $staging 'build'),
    (Join-Path $PSScriptRoot 'workbench.spec')
)
$originalPath = $env:PATH
try {
    # Keep native DLL discovery away from other applications on the build host.
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
} finally {
    $env:PATH = $originalPath
}
Write-Output "Private build candidate: $staging"
