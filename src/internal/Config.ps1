function Merge-Config {
    param([hashtable]$Base, [hashtable]$Overlay)

    $out = @{}
    foreach ($k in $Base.Keys) {
        $out[$k] = if ($Base[$k] -is [hashtable]) { Merge-Config -Base $Base[$k] -Overlay @{} } else { $Base[$k] }
    }
    foreach ($k in $Overlay.Keys) {
        if ($null -eq $Overlay[$k]) { continue }
        $out[$k] = if ($Overlay[$k] -is [hashtable] -and $out[$k] -is [hashtable]) {
            Merge-Config -Base $out[$k] -Overlay $Overlay[$k]
        } else { $Overlay[$k] }
    }
    return $out
}

function Get-Cfg {
    param($Config, [string]$Path, $Default = $null)

    $cur = $Config
    foreach ($key in $Path.Split('.')) {
        if ($cur -is [hashtable] -and $cur.ContainsKey($key)) { $cur = $cur[$key] } else { return $Default }
    }
    if ($null -eq $cur) { return $Default }
    return $cur
}

function ConvertTo-Hashtable {
    param($Object)

    if ($null -eq $Object) { return $null }
    if ($Object -is [hashtable]) { return $Object }
    if ($Object -isnot [pscustomobject]) { return $Object }

    $h = @{}
    foreach ($p in $Object.PSObject.Properties) { $h[$p.Name] = ConvertTo-Hashtable $p.Value }
    return $h
}

function Get-DefaultConfig {
    return @{
        url      = ''
        site     = 'generic'
        port     = 9222
        plan     = ''
        targetStage = 'S4'
        seco     = $false
        beep     = $true
        browser  = @{ channel = 'auto'; profile = ''; headless = $false; args = @() }
        timing   = @{ quiet = 10; poll = 3; watchdog = 8; captchaGrace = 60; verdictForce = 5; sendSettle = 1600 }
        limits   = @{
            turns = 0; cards = 40; redsPerPhase = 3; deepRetries = 8; cleanAudits = 3
            maxPromptChars = 9000; chunkDelay = 1200; wallMinutes = 0
            maxRegressions = 3; rollbackOnRegression = $true
        }
        contract = @{ header = $true }
        audits   = @('form sweep', 'route sweep', 'evidence sweep')
    }
}

function Resolve-AutonomaConfig {
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)][hashtable]$Cli)

    $cfg = Get-DefaultConfig

    $configPath = if ($Cli.config) { $Cli.config } else { Join-Path $Root 'config/autonoma.json' }
    if (Test-Path -LiteralPath $configPath) {
        $raw = Get-Content -Raw -Encoding UTF8 -LiteralPath $configPath
        $cfg = Merge-Config -Base $cfg -Overlay (ConvertTo-Hashtable ($raw | ConvertFrom-Json))
    }

    # Environment variables are the lowest-priority override, above defaults.
    foreach ($pair in @(
            @('AUTONOMA_URL', 'url'), @('AUTONOMA_SITE', 'site'), @('AUTONOMA_PLAN', 'plan'),
            @('AUTONOMA_PROFILE', 'browser.profile'), @('AUTONOMA_PORT', 'port'), @('AUTONOMA_STAGE', 'targetStage')
        )) {
        $value = [Environment]::GetEnvironmentVariable($pair[0])
        if ([string]::IsNullOrWhiteSpace($value)) { continue }
        Set-CfgValue -Config $cfg -Path $pair[1] -Value $value
    }

    # Command-line flags win over everything else.
    if ($Cli.url) { $cfg.url = $Cli.url }
    if ($Cli.site) { $cfg.site = $Cli.site }
    if ($Cli.plan) { $cfg.plan = $Cli.plan }
    if ($Cli.profile) { $cfg.browser.profile = $Cli.profile }
    if ($Cli.browser) { $cfg.browser.channel = $Cli.browser }
    if ($Cli.port -gt 0) { $cfg.port = $Cli.port }
    if ($Cli.maxTurns -gt 0) { $cfg.limits.turns = $Cli.maxTurns }
    if ($Cli.maxCards -gt 0) { $cfg.limits.cards = $Cli.maxCards }
    if ($Cli.targetStage) { $cfg.targetStage = $Cli.targetStage }
    if ($Cli.headless) { $cfg.browser.headless = $true }
    if ($Cli.noBeep) { $cfg.beep = $false }

    $cfg.rootDir = $Root
    $cfg.planPath = if ($cfg.plan) {
        if ([IO.Path]::IsPathRooted($cfg.plan)) { $cfg.plan } else { Join-Path $Root $cfg.plan }
    } else { '' }

    $sitePath = Join-Path $Root ('config/sites/' + $cfg.site + '.json')
    if (-not (Test-Path -LiteralPath $sitePath)) { throw ('unknown site adapter: ' + $sitePath) }
    $cfg.siteAdapter = ConvertTo-Hashtable (Get-Content -Raw -Encoding UTF8 -LiteralPath $sitePath | ConvertFrom-Json)

    $weights = (Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'config/scorecard.json') | ConvertFrom-Json)
    $cfg.weights = ConvertTo-Hashtable $weights.axes
    $cfg.tolerance = [double]$weights.tolerance
    $cfg.stages = ConvertTo-Hashtable (Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $Root 'config/stages.json') | ConvertFrom-Json)

    $home_ = Join-Path $env:LOCALAPPDATA 'Autonoma'
    $cfg.homeDir = $home_
    $cfg.stateDir = Join-Path $home_ 'state'
    $cfg.logDir = Join-Path $home_ 'logs'
    if (-not $cfg.browser.profile) { $cfg.browser.profile = Join-Path $home_ 'profile' }
    New-Item -ItemType Directory -Force -Path $cfg.stateDir, $cfg.logDir, $cfg.browser.profile | Out-Null
    return $cfg
}

function Set-CfgValue {
    param([hashtable]$Config, [string]$Path, $Value)

    $parts = $Path.Split('.')
    $cur = $Config
    for ($i = 0; $i -lt $parts.Count - 1; $i++) {
        if (-not ($cur[$parts[$i]] -is [hashtable])) { $cur[$parts[$i]] = @{} }
        $cur = $cur[$parts[$i]]
    }
    $cur[$parts[-1]] = $Value
}
