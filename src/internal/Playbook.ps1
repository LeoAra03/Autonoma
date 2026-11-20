function Get-Playbook {
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)][string]$Id)

    $manifest = Get-ChildItem -LiteralPath (Join-Path $Root 'playbooks') -Recurse -Filter 'manifest.json' |
        ForEach-Object {
            $m = ConvertTo-Hashtable (Get-Content -Raw -Encoding UTF8 -LiteralPath $_.FullName | ConvertFrom-Json)
            [pscustomobject]@{ Path = $_.FullName; Doc = $m }
        } |
        Where-Object { $_.Doc.id -eq $Id } |
        Select-Object -First 1
    if (-not $manifest) { return $null }

    $doc = $manifest.Doc
    $briefs = @(Get-ChildItem -LiteralPath $manifest.DirectoryName -Recurse -Filter '*.md' | Sort-Object Name | ForEach-Object { Get-Content -Raw -Encoding UTF8 -LiteralPath $_.FullName })
    return [pscustomobject]@{ id = $doc.id; axis = $doc.axis; briefs = $briefs }
}

function Get-Telemetry {
    param([hashtable]$Config)

    $path = Join-Path $Config.stateDir 'telemetry.jsonl'
    if (-not (Test-Path -LiteralPath $path)) { return @() }
    return @(Get-Content -LiteralPath $path -Encoding UTF8 | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
}

function Add-Telemetry {
    param([hashtable]$Config, [hashtable]$Entry)

    $path = Join-Path $Config.stateDir 'telemetry.jsonl'
    $Entry.at = (Get-Date).ToString('s')
    Add-Content -LiteralPath $path -Value ($Entry | ConvertTo-Json -Compress) -Encoding UTF8
}

function Get-WeakPlaybooks {
    # Playbooks that fail far more often than they pass are candidates for rework.
    param([hashtable]$Config, [double]$Threshold = 0.6)

    $stats = @{}
    foreach ($t in (Get-Telemetry -Config $Config)) {
        if (-not $stats.ContainsKey($t.phase)) { $stats[$t.phase] = @{ red = 0; total = 0 } }
        $stats[$t.phase].total++
        if ($t.verdict -eq 'rojo') { $stats[$t.phase].red++ }
    }
    return @($stats.Keys | Where-Object { $stats[$_].total -ge 5 -and ($stats[$_].red / $stats[$_].total) -ge $Threshold })
}
