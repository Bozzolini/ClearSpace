# Verifies a downloaded ClearSpace zip against the official SHA256SUMS file.
# Usage:  powershell -File scripts\verify_release.ps1 -Zip .\ClearSpace-2.0.0-win64.zip -Sums .\SHA256SUMS
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [Parameter(Mandatory = $true)][string]$Sums
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Zip)) { throw "Zip not found: $Zip" }
if (-not (Test-Path -LiteralPath $Sums)) { throw "SHA256SUMS not found: $Sums" }

$name = Split-Path -Leaf $Zip
$escaped = [regex]::Escape($name)
$expected = $null
foreach ($line in Get-Content -LiteralPath $Sums) {
    if ($line -match "^([0-9a-fA-F]{64})\s+\*?$escaped$") {
        $expected = $Matches[1].ToLower()
    }
}
if (-not $expected) { throw "No hash for $name in $Sums. Do not run this archive." }

$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Zip).Hash.ToLower()
if ($actual -ne $expected) {
    Write-Error "HASH MISMATCH. Do not unzip or run this file.`nExpected $expected`nActual   $actual"
    exit 1
}

Write-Host "Hash OK: $name"
Write-Host "The archive matches SHA256SUMS. Extract it and run ClearSpace\ClearSpace.exe."
Write-Host "If Windows SmartScreen warns, that is because the exe is not code-signed. Do not turn SmartScreen off; download again from the official release if you did not expect the warning."
exit 0
