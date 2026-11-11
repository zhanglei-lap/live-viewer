$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
python (Join-Path $PSScriptRoot 'build-windows.py')
if ($LASTEXITCODE -ne 0) { throw 'Windows package build failed.' }
