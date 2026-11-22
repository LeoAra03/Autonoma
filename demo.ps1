#Requires -Version 5.1
<#
.SYNOPSIS
    Demo: run the Autonoma self-check from Windows PowerShell.

.DESCRIPTION
    Executes tools/verify.ps1 and reports the exit code. Use this to sanity-check
    the repository before pushing on Windows.

.EXAMPLE
    ./demo.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

Write-Host '== Autonoma demo (PowerShell) =='
& "$root/tools/verify.ps1"
$code = $LASTEXITCODE
Write-Host ('Exit code: ' + $code)
exit $code
