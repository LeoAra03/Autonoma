#Requires -Version 5.1
<#
.SYNOPSIS
    Autonoma - deterministic work director.

.DESCRIPTION
    Drives a web chat agent through a dependency-ordered plan, gated by
    external commands and scored against a stage ladder.

.EXAMPLE
    .\autonoma.ps1 -Url 'https://site/chat/1' -Plan plans\example\roadmap

.EXAMPLE
    .\autonoma.ps1 -Discover -Url 'https://site/chat/1'
#>
[CmdletBinding()]
param(
    [string]$Url,
    [string]$Config,
    [string]$Site,
    [string]$Plan,
    [int]$Port = 0,
    [int]$MaxTurns = 0,
    [int]$MaxCards = 0,
    [string]$Profile,
    [string]$Browser,
    [string]$TargetStage,
    [switch]$Discover,
    [switch]$DryRun,
    [switch]$Once,
    [switch]$Headless,
    [switch]$Reset,
    [switch]$ListPhases,
    [switch]$Score,
    [switch]$Stage,
    [switch]$VerifyLedger,
    [switch]$NoBeep,
    [switch]$Version
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Autonoma.psm1') -Force

if ($Version) { Write-Host ('Autonoma ' + (Get-AutonomaVersion)); return }

$cli = @{
    url = $Url; config = $Config; site = $Site; plan = $Plan; port = $Port
    maxTurns = $MaxTurns; maxCards = $MaxCards; profile = $Profile
    browser = $Browser; targetStage = $TargetStage
    discover = [bool]$Discover; dryRun = [bool]$DryRun; once = [bool]$Once
    headless = [bool]$Headless; reset = [bool]$Reset; list = [bool]$ListPhases
    score = [bool]$Score; stage = [bool]$Stage
    verifyLedger = [bool]$VerifyLedger; noBeep = [bool]$NoBeep
}

$root = Split-Path -Parent $PSScriptRoot
$cfg = Resolve-AutonomaConfig -Root $root -Cli $cli

if ($VerifyLedger) {
    $ok = Test-Ledger -Config $cfg
    Write-Host $(if ($ok) { 'ledger intact' } else { 'ledger BROKEN' })
    exit $(if ($ok) { 0 } else { 1 })
}

if ($Discover) {
    $generated = Invoke-Discovery -Config $cfg
    Write-Host ('plan generated: ' + $generated)
    return
}

if (-not $cfg.planPath) { throw 'no plan given (use -Plan or --discover)' }

$phases = Read-Roadmap -Path $cfg.planPath

if ($ListPhases) { foreach ($p in $phases) { Write-Host ($p.id + ' [' + $p.axis + '] ' + $p.title) }; return }

if ($Reset) { Remove-PlanState -PlanPath $cfg.planPath; Write-Host 'state cleared'; return }

$state = Read-PlanState -PlanPath $cfg.planPath

if ($Score) {
    $card = Get-Scorecard -Results (Get-LedgerResults -Config $cfg -Phase '*') -Weights $cfg.weights
    $card | ConvertTo-Json -Depth 4
    return
}

if ($Stage) { Write-Host ('stage ' + $state.stage); return }

if ($DryRun) { Show-PlanDryRun -Config $cfg -Phases $phases; return }

Invoke-AutonomaEngine -Config $cfg -Phases $phases
