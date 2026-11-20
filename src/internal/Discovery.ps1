function Get-StackProfile {
    param([Parameter(Mandatory)][string]$Root)

    $files = Get-TrackedFiles -Root $Root
    $markers = @{
        'node'    = @('package.json')
        'python'  = @('pyproject.toml', 'requirements.txt')
        'go'      = @('go.mod')
        'rust'    = @('Cargo.toml')
        'dotnet'  = @('*.csproj', '*.sln')
        'web'     = @('**/*.html', '**/*.tsx', '**/*.jsx')
        'cli'     = @('**/bin/**', '**/cmd/**')
        'data'    = @('**/*.ipynb', '**/dbt_project.yml')
    }

    $found = @()
    foreach ($key in $markers.Keys) {
        foreach ($glob in $markers[$key]) {
            if (@($files | Where-Object { Test-Glob -Glob $glob -Path $_ }).Count -gt 0) { $found += $key; break }
        }
    }
    return [pscustomobject]@{
        markers   = $found
        fileCount = $files.Count
        hasTests  = (@($files | Where-Object { $_ -match '(test|spec)' }).Count -gt 0)
        hasCi     = (@($files | Where-Object { $_ -like '.github/workflows/*' }).Count -gt 0)
        hasLicense = (@($files | Where-Object { $_ -like 'LICENSE*' }).Count -gt 0)
        hasReadme = (@($files | Where-Object { $_ -like 'README*' }).Count -gt 0)
        lockfiles = @($files | Where-Object { $_ -match '(package-lock\.json|poetry\.lock|Cargo\.lock|go\.sum)$' })
    }
}

function New-RiskRegister {
    param([pscustomobject]$Profile, [Parameter(Mandatory)][string]$OutDir)

    $risks = New-Object System.Collections.Generic.List[string]
    if (-not $Profile.hasTests) { $risks.Add('HIGH no test runner detected: nothing can be gated on behaviour') }
    if (-not $Profile.hasCi) { $risks.Add('HIGH no CI: gates will only run locally') }
    if (-not $Profile.hasLicense) { $risks.Add('MEDIUM no license file: redistribution unclear') }
    if (-not $Profile.hasReadme) { $risks.Add('MEDIUM no README: no entry point for a stranger') }
    if ($Profile.lockfiles.Count -eq 0 -and $Profile.markers -contains 'node') { $risks.Add('HIGH no lockfile: dependency set is not reproducible') }

    $path = Join-Path $OutDir 'RISKS.md'
    $body = @('# Risk register', '', ('generated ' + (Get-Date).ToString('s')), '') + @($risks | ForEach-Object { '- ' + $_ })
    Set-Content -LiteralPath $path -Value ($body -join "`n") -Encoding UTF8
    return $risks.ToArray()
}

function Get-PlaybookForProfile {
    param([pscustomobject]$Profile, [Parameter(Mandatory)][string]$Root)

    $dir = Join-Path $Root 'playbooks'
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($manifest in (Get-ChildItem -LiteralPath $dir -Recurse -Filter 'manifest.json')) {
        # A hashtable, so a manifest without a "when" key reads as an empty list.
        $doc = ConvertTo-Hashtable (Get-Content -Raw -Encoding UTF8 -LiteralPath $manifest.FullName | ConvertFrom-Json)
        # Archetype playbooks match when any marker appears in any "when" pattern;
        # universal playbooks always apply.
        $hit = @($doc.when | Where-Object { $g = $_; @($Profile.markers | Where-Object { $g -like ('*' + $_ + '*') }).Count -gt 0 }).Count -gt 0
        if ($hit -or $doc.universal -eq $true) {
            $out.Add([pscustomobject]@{ id = $doc.id; axis = $doc.axis; stages = @($doc.stages); briefs = @($doc.briefs) })
        }
    }
    return $out.ToArray()
}

function Invoke-Discovery {
    param([Parameter(Mandatory)][hashtable]$Config)

    $profile = Get-StackProfile -Root $Config.rootDir
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $outDir = Join-Path $Config.rootDir ('plans/discovered-' + $stamp)
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null

    $risks = New-RiskRegister -Profile $profile -OutDir $outDir
    Write-Log ('stack: ' + ($profile.markers -join ',') + '; risks: ' + $risks.Count)

    $playbooks = Get-PlaybookForProfile -Profile $profile -Root $Config.rootDir
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# id|axis|depends|type|title|criterion')
    $previous = ''
    foreach ($pb in $playbooks) {
        $lines.Add($pb.id + '|' + $pb.axis + '|' + $previous + '|PLAYBOOK|' + $pb.id + '|gate passes for ' + $pb.axis)
        $previous = $pb.id
    }

    $roadmap = Join-Path $outDir 'roadmap'
    Set-Content -LiteralPath $roadmap -Value ($lines -join "`n") -Encoding UTF8

    $briefDir = Join-Path $outDir 'briefs'
    New-Item -ItemType Directory -Force -Path $briefDir | Out-Null
    foreach ($pb in $playbooks) {
        $phaseDir = Join-Path $briefDir $pb.id
        New-Item -ItemType Directory -Force -Path $phaseDir | Out-Null
        $index = 1
        foreach ($brief in $pb.briefs) {
            Set-Content -LiteralPath (Join-Path $phaseDir ('{0:D2}.md' -f $index)) -Value $brief -Encoding UTF8
            $index++
        }
    }

    $card = Get-Scorecard -Results @() -Weights $Config.weights
    $card | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $outDir 'scorecard.baseline.json') -Encoding UTF8

    return $roadmap
}
