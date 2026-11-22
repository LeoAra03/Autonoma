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
$logPath = Join-Path$root 'verify.log'
try { Remove-Item -LiteralPath $logPath -ErrorAction SilentlyContinue } catch { }
function Write-Log {
    param([string]$Text)
    Write-Host $Text
    Add-Content -LiteralPath $logPath -Value$Text -Encoding UTF8
}
Import-Module (Join-Path $root 'src/Autonoma.psm1') -Force

$fail = 0

Write-Log '== pester =='
Import-Module Pester -MinimumVersion 5.5.0 -ErrorAction Stop
$result = Invoke-Pester -Path (Join-Path $root 'tests') -PassThru -Output None
if ($result.FailedCount -gt 0) { $fail++; Write-Log ($result.FailedCount.ToString() + ' failed') }
else { Write-Log ($result.PassedCount.ToString() + ' passed') }

Write-Log '== analyzer =='
$issues = Invoke-ScriptAnalyzer -Path (Join-Path $root 'src') -Recurse -Severity Error
if ($issues) { $fail++; $issues | Format-Table -AutoSize | Out-String | Write-Log }
else { Write-Log 'clean' }

Write-Log '== plan =='
$plan = Join-Path $root 'plans/example/roadmap'
$phases = Read-Roadmap -Path $plan
if ($phases.Count -eq 0) { $fail++; Write-Log 'empty plan' }
$cycle = Test-GraphCycles -Phases $phases
if ($cycle) { $fail++; Write-Log $cycle } else { Write-Log ($phases.Count.ToString() + ' phases, acyclic') }

Write-Log '== gates =='
$cfg = @{ rootDir = $root }
$defs = Get-GateDefinition -Config $cfg
$ids = @($defs | ForEach-Object { $_.id })
$dupes = @($ids | Group-Object | Where-Object { $_.Count -gt 1 })
if ($dupes.Count -gt 0) { $fail++; Write-Log ('duplicate gate ids: ' + ($dupes.Name -join ',')) }
foreach ($d in $defs) {
    if (-not $d.severity -or -not $d.weight -or -not $d.timeout) { $fail++; Write-Log ('incomplete gate ' + $d.id) }
}
Write-Log ($ids.Count.ToString() + ' gates')

Write-Log '== docs links =='
$broken = 0
$mdFiles = Get-ChildItem -LiteralPath$root -Recurse -Filter '*.md' -File |
    Where-Object { $_.FullName -notmatch '[\\/](\.git|node_modules|bin|obj|artifacts)[\\/]' }
foreach ($md in$mdFiles) {
    $text = Get-Content -Raw -Encoding UTF8 -LiteralPath$md.FullName
    foreach ($m in [regex]::Matches($text, '\]\((?!https?://|mailto:|tel:|#)([^)\s]+)')) {
        $rel = [uri]::UnescapeDataString($m.Groups[1].Value)
        if ($rel -match '^[A-Za-z]:' -or $rel.StartsWith('/')) { continue }
        $target = Join-Path $md.DirectoryName $rel
        if (-not (Test-Path -LiteralPath $target)) {
            $broken++
            Write-Log ('broken: ' + $md.FullName.Substring($root.Length + 1) + ' -> ' + $rel)
        }
    }
}
if ($broken -gt 0) { $fail++; Write-Log ($broken.ToString() + ' broken doc links') }
else { Write-Log 'clean' }

Write-Log ''
Write-Log ('verify finished with ' + $fail.ToString() + ' failed groups')
if ($fail -gt 0) { Write-Log ($fail.ToString() + ' check groups failed'); exit 1 }
Write-Log 'all checks green'
