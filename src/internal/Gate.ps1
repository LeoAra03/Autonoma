function Get-GateDefinition {
    param([Parameter(Mandatory)][hashtable]$Config)

    $dir = Join-Path $Config.rootDir 'config/gates'
    if (-not (Test-Path -LiteralPath $dir)) { return @() }

    $all = New-Object System.Collections.Generic.List[object]
    foreach ($file in (Get-ChildItem -LiteralPath $dir -Filter '*.json')) {
        $doc = Get-Content -Raw -Encoding UTF8 -LiteralPath $file | ConvertFrom-Json
        foreach ($gate in $doc.gates) {
            $entry = ConvertTo-Hashtable $gate
            $entry.axis = $doc.axis
            $entry.when = @($entry.when)
            # Normalize optional keys so strict mode never trips on a missing property.
            if (-not $entry.ContainsKey('expect')) { $entry.expect = @{ exit = 0 } }
            else { $entry.expect = ConvertTo-Hashtable $entry.expect }
            if (-not $entry.ContainsKey('artifact')) { $entry.artifact = '' }
            $all.Add([pscustomobject]$entry)
        }
    }
    return $all.ToArray()
}

function Select-ApplicableGates {
    param([object[]]$Gates, [string[]]$Files, [string]$Axis = '')

    return @($Gates | Where-Object {
            ($Axis -eq '' -or $_.axis -eq $Axis) -and
            @($_.when | Where-Object { $g = $_; @($Files | Where-Object { Test-Glob -Glob $g -Path $_ }).Count -gt 0 }).Count -gt 0
        })
}

function Get-TrackedFiles {
    param([Parameter(Mandatory)][string]$Root)

    # Prefer the git index; fall back to a recursive scan when the root is not
    # a repo or git is not installed.
    Push-Location $Root
    try {
        try {
            $git = git ls-files 2>$null
            if ($LASTEXITCODE -eq 0 -and $git) { return @($git) }
        } catch { }
    } finally { Pop-Location }

    return @(Get-ChildItem -LiteralPath $Root -Recurse -File |
        Where-Object { $_.FullName -notmatch '\\(\.git|node_modules|bin|obj|dist)\\' } |
        ForEach-Object { $_.FullName.Substring($Root.Length + 1) })
}

function Resolve-GateTokens {
    param([string]$Command, [hashtable]$Config)

    if ($Command -notmatch '\{lockfile\}') { return $Command }
    $candidates = @('package-lock.json', 'poetry.lock', 'Cargo.lock', 'go.sum')
    foreach ($name in $candidates) {
        $path = Join-Path $Config.rootDir $name
        if (Test-Path -LiteralPath $path) { return $Command.Replace('{lockfile}', $name) }
    }
    return $Command
}

function Invoke-Gate {
    param(
        [Parameter(Mandatory)]$Gate,
        [Parameter(Mandatory)][hashtable]$Config,
        [Parameter(Mandatory)][string]$ArtifactDir
    )

    $result = [pscustomobject]@{
        id = $Gate.id; axis = $Gate.axis; severity = $Gate.severity; weight = [int]$Gate.weight
        status = 'pass'; exit = 0; reason = ''; artifact = ''; sha256 = ''; ms = 0
    }

    $command = Resolve-GateTokens -Command $Gate.cmd -Config $Config
    $result.artifact = if ($Gate.artifact) { Join-Path $ArtifactDir $Gate.artifact } else { '' }
    $command = $command.Replace('{artifact}', $result.artifact)

    # A missing tool skips the gate; it never silently passes it.
    $exe = ($command -split ' ', 2)[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
        $result.status = 'skipped'
        $result.reason = 'missing tool: ' + $exe
        return $result
    }

    $clock = [Diagnostics.Stopwatch]::StartNew()
    $out = Join-Path $ArtifactDir ($Gate.id + '.out.txt')
    $err = Join-Path $ArtifactDir ($Gate.id + '.err.txt')

    try {
        $proc = Start-Process -FilePath 'powershell.exe' -PassThru -NoNewWindow `
            -ArgumentList @('-NoProfile', '-Command', $command) `
            -WorkingDirectory $Config.rootDir -RedirectStandardOutput $out -RedirectStandardError $err
        if (-not $proc.WaitForExit([int]$Gate.timeout * 1000)) {
            try { $proc.Kill() } catch { }
            $result.status = 'fail'
            $result.reason = 'timeout ' + $Gate.timeout + 's'
        } else { $result.exit = $proc.ExitCode }
    } catch {
        $result.status = 'fail'
        $result.reason = $_.Exception.Message
    }
    $clock.Stop()
    $result.ms = $clock.ElapsedMilliseconds
    if ($result.status -eq 'fail') { return $result }

    $want = if ($Gate.expect.ContainsKey('exit')) { [int]$Gate.expect['exit'] } else { 0 }
    if ($result.exit -ne $want) {
        $result.status = 'fail'
        $result.reason = 'exit ' + $result.exit + ' want ' + $want
        return $result
    }

    # Optional JSON expectation: walk a dotted path and bound the result count.
    if ($Gate.expect.ContainsKey('json') -and (Test-Path -LiteralPath $out)) {
        try {
            $node = (Get-Content -Raw -Encoding UTF8 -LiteralPath $out | ConvertFrom-Json)
            foreach ($step in $Gate.expect['json']['path'].Split('.')) { $node = $node.$step }
            if (@($node).Count -gt [int]$Gate.expect['json']['max']) {
                $result.status = 'fail'
                $result.reason = @($node).Count.ToString() + ' findings'
                return $result
            }
        } catch {
            $result.status = 'fail'
            $result.reason = 'unparsable json output'
            return $result
        }
    }

    if ($result.artifact -and (Test-Path -LiteralPath $result.artifact)) {
        $result.sha256 = (Get-FileHash -LiteralPath $result.artifact -Algorithm SHA256).Hash
    }
    return $result
}

function Invoke-GateSet {
    param([Parameter(Mandatory)][hashtable]$Config, [Parameter(Mandatory)][string]$PhaseId, [string]$Axis)

    $files = Get-TrackedFiles -Root $Config.rootDir
    $gates = Select-ApplicableGates -Gates (Get-GateDefinition -Config $Config) -Files $files -Axis $Axis
    $dir = Join-Path $Config.stateDir ('gates/' + $PhaseId + '-' + (Get-Date -Format 'HHmmss'))
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    $results = New-Object System.Collections.Generic.List[object]
    foreach ($gate in $gates) {
        $r = Invoke-Gate -Gate $gate -Config $Config -ArtifactDir $dir
        $results.Add($r)
        Write-Log ('gate ' + $gate.id + ' ' + $r.status + ' ' + $r.reason) $(if ($r.status -eq 'fail') { 'warn' } else { 'debug' })
    }

    return [pscustomobject]@{
        phase   = $PhaseId
        axis    = $Axis
        dir     = $dir
        results = $results.ToArray()
        blocked = (@($results | Where-Object { $_.status -eq 'fail' -and $_.severity -eq 'blocker' }).Count -gt 0)
        failed  = (@($results | Where-Object { $_.status -eq 'fail' }).Count -gt 0)
        skipped = (@($results | Where-Object { $_.status -eq 'skipped' }).Count)
    }
}
