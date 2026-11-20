#Requires -Version 5.1
<#
.SYNOPSIS
    Full self-check: tests, analyzer, plan integrity, gate definitions, docs links.

.DESCRIPTION
    This is the same gate CI runs. Run it before every push.
#>
[CmdletBinding()]
param([switch]$Strict)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Import-Module (Join-Path $root 'src/Autonoma.psm1') -Force

$fail = 0

Write-Host '== pester =='
Import-Module Pester -MinimumVersion 5.5.0 -ErrorAction Stop
$result = Invoke-Pester -Path (Join-Path $root 'tests') -PassThru -Output None
if ($result.FailedCount -gt 0) { $fail++; Write-Host ($result.FailedCount.ToString() + ' failed') -ForegroundColor Red }
else { Write-Host ($result.PassedCount.ToString() + ' passed') }

Write-Host '== analyzer =='
$issues = Invoke-ScriptAnalyzer -Path (Join-Path $root 'src') -Recurse -Severity Error
if ($issues) { $fail++; $issues | Format-Table -AutoSize | Out-String | Write-Host }
else { Write-Host 'clean' }

Write-Host '== plan =='
$plan = Join-Path $root 'plans/example/roadmap'
$phases = Read-Roadmap -Path $plan
if ($phases.Count -eq 0) { $fail++; Write-Host 'empty plan' -ForegroundColor Red }
$cycle = Test-GraphCycles -Phases $phases
if ($cycle) { $fail++; Write-Host $cycle -ForegroundColor Red } else { Write-Host ($phases.Count.ToString() + ' phases, acyclic') }

Write-Host '== gates =='
$cfg = @{ rootDir = $root }
$defs = Get-GateDefinition -Config $cfg
$ids = @($defs | ForEach-Object { $_.id })
$dupes = @($ids | Group-Object | Where-Object { $_.Count -gt 1 })
if ($dupes.Count -gt 0) { $fail++; Write-Host ('duplicate gate ids: ' + ($dupes.Name -join ',')) -ForegroundColor Red }
foreach ($d in $defs) {
    if (-not $d.severity -or -not $d.weight -or -not $d.timeout) { $fail++; Write-Host ('incomplete gate ' + $d.id) -ForegroundColor Red }
}
Write-Host ($ids.Count.ToString() + ' gates')

Write-Host '== docs links =='
$broken = 0
foreach ($md in (Get-ChildItem -LiteralPath $root -Recurse -Filter '*.md' -File)) {
    foreach ($m in [regex]::Matches((Get-Content -Raw -Encoding UTF8 -LiteralPath $md.FullName), '\]\((?!http)([^)#]+)')) {
        $target = Join-Path $md.DirectoryName $m.Groups[1].Value
        if (-not (Test-Path -LiteralPath $target)) { $broken++; Write-Host ('broken: ' + $md.Name + ' -> ' + $m.Groups[1].Value) -ForegroundColor Red }
    }
}
if ($broken -gt 0) { $fail++ }

Write-Host ''
if ($fail -gt 0) { Write-Host ($fail.ToString() + ' check groups failed') -ForegroundColor Red; exit 1 }
Write-Host 'all checks green'
