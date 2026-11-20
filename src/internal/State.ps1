function Get-PlanStatePath {
    param([Parameter(Mandatory)][string]$PlanPath)

    # State is keyed by the plan path and its content hash, so editing a plan
    # starts a fresh run instead of reusing someone else's state.
    $hash = (Get-FileHash -LiteralPath $PlanPath -Algorithm SHA1).Hash.Substring(0, 8).ToLowerInvariant()
    $name = [IO.Path]::GetFileNameWithoutExtension($PlanPath)
    return (Join-Path $env:LOCALAPPDATA ('Autonoma/state/' + $name + '-' + $hash + '.json'))
}

function Read-PlanState {
    param([Parameter(Mandatory)][string]$PlanPath)

    $state = @{
        version = 1; turns = 0; cards = 0; cycle = 0; clean = 0; regressions = 0
        stage = 'S0'; startedAt = (Get-Date).ToString('s'); pending = ''; lastHash = ''
        auditId = ''; startSha = ''; states = @{}; parts = @{}; passes = @{}
    }

    $path = Get-PlanStatePath -PlanPath $PlanPath
    if (-not (Test-Path -LiteralPath $path)) { return $state }

    try {
        $raw = Get-Content -Raw -Encoding UTF8 -LiteralPath $path | ConvertFrom-Json
        foreach ($k in @('version', 'turns', 'cards', 'cycle', 'clean', 'regressions', 'stage', 'startedAt', 'pending', 'lastHash', 'auditId', 'startSha')) {
            if ($null -ne $raw.$k) { $state[$k] = $raw.$k }
        }
        foreach ($map in @('states', 'parts', 'passes')) {
            $src = $raw.$map
            if ($null -eq $src) { continue }
            foreach ($p in $src.PSObject.Properties) { $state[$map][$p.Name] = $p.Value }
        }
    } catch {
        Write-Log ('state unreadable, starting clean: ' + $_.Exception.Message) 'warn'
    }
    return $state
}

function Save-PlanState {
    param([Parameter(Mandatory)][string]$PlanPath, [Parameter(Mandatory)][hashtable]$State)

    $path = Get-PlanStatePath -PlanPath $PlanPath
    $tmp = $path + '.tmp'
    try {
        # Write to a temp file and swap: a crash mid-save never leaves a half-written state.
        $State | ConvertTo-Json -Depth 6 -Compress | Set-Content -LiteralPath $tmp -Encoding UTF8
        Move-Item -LiteralPath $tmp -Destination $path -Force
    } catch {
        Write-Log ('state save failed: ' + $_.Exception.Message) 'warn'
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Remove-PlanState {
    param([Parameter(Mandatory)][string]$PlanPath)
    $path = Get-PlanStatePath -PlanPath $PlanPath
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
}
