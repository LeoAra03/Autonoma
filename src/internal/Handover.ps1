$script:HandoverFiles = @(
    'ARCHITECTURE.md', 'RUNBOOK.md', 'OWNERS.md', 'ONBOARDING.md',
    'TRANSFER.md', 'SECURITY.md', 'NOTICE', 'SBOM.json'
)

function Get-HandoverReport {
    param([Parameter(Mandatory)][string]$Root)

    $rows = foreach ($name in $script:HandoverFiles) {
        $candidates = @(Get-ChildItem -LiteralPath $Root -Recurse -Filter $name -File -ErrorAction SilentlyContinue)
        $first = $candidates | Select-Object -First 1
        [pscustomobject]@{
            file     = $name
            present  = [bool]$first
            nonEmpty = if ($first) { $first.Length -gt 200 } else { $false }
        }
    }
    $rows = @($rows)
    return [pscustomobject]@{
        rows      = $rows
        complete  = (@($rows | Where-Object { -not $_.present -or -not $_.nonEmpty }).Count -eq 0)
        missing   = @($rows | Where-Object { -not $_.present } | ForEach-Object { $_.file })
    }
}

function New-HandoverCheck {
    param([Parameter(Mandatory)][hashtable]$Config)

    $report = Get-HandoverReport -Root $Config.rootDir
    $path = Join-Path $Config.stateDir 'handover.json'
    $report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $path -Encoding UTF8
    return $report
}
