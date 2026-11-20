function ConvertFrom-RoadmapLine {
    param([string]$Line)

    $text = $Line.Trim()
    if ($text -eq '' -or $text.StartsWith('#')) { return $null }

    $parts = $text -split '\|', 6
    if ($parts.Count -lt 6) { return $null }

    $id = $parts[0].Trim()
    if ($id -notmatch '^[A-Za-z0-9_-]{1,16}$') { return $null }

    $depends = @($parts[2].Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })

    return [pscustomobject]@{
        id        = $id
        axis      = $parts[1].Trim()
        depends   = $depends
        type      = $parts[3].Trim()
        title     = $parts[4].Trim()
        criterion = $parts[5].Trim()
    }
}

function Read-Roadmap {
    param([Parameter(Mandatory)][string]$Path)

    $out = New-Object System.Collections.Generic.List[object]
    if (-not (Test-Path -LiteralPath $Path)) { return $out.ToArray() }
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $p = ConvertFrom-RoadmapLine -Line $line
        if ($p) { $out.Add($p) }
    }
    return $out.ToArray()
}

function Get-BriefParts {
    param([string]$PlanPath, [string]$PhaseId)

    $dir = Join-Path (Join-Path (Split-Path -Parent $PlanPath) 'briefs') $PhaseId
    if (-not (Test-Path -LiteralPath $dir)) { return @() }
    return @(Get-ChildItem -LiteralPath $dir -Filter '*.md' | Sort-Object Name | ForEach-Object { $_.FullName })
}

function Get-PartCount {
    param([string]$PlanPath, [string]$PhaseId)
    return (Get-BriefParts -PlanPath $PlanPath -PhaseId $PhaseId).Count
}

function Get-BriefBody {
    param([string]$PlanPath, [string]$PhaseId, [int]$Index)

    $files = Get-BriefParts -PlanPath $PlanPath -PhaseId $PhaseId
    if ($Index -lt 1 -or $Index -gt $files.Count) { return '' }
    return (Get-Content -Raw -Encoding UTF8 -LiteralPath $files[$Index - 1]).Trim()
}

function Show-PlanDryRun {
    param([hashtable]$Config, [object[]]$Phases)

    Write-Host ('plan: ' + $Config.planPath)
    Write-Host ('target stage: ' + $Config.targetStage)
    foreach ($p in $Phases) {
        $count = Get-PartCount -PlanPath $Config.planPath -PhaseId $p.id
        Write-Host ('  ' + $p.id + ' [' + $p.axis + '] ' + $p.title + ' deps=' + ($p.depends -join ',') + ' parts=' + $count)
    }
    Write-Host ('total phases: ' + $Phases.Count)
}
