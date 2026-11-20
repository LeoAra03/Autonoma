function Invoke-AutonomaEngine {
    param([Parameter(Mandatory)][hashtable]$Config, [Parameter(Mandatory)][object[]]$Phases)

    $script:BeepEnabled = [bool](Get-Cfg $Config 'beep' $true)
    $script:JsDir = Join-Path $Config.rootDir 'src/js'

    $cycle = Test-GraphCycles -Phases $Phases
    if ($cycle) { Write-Log ('plan invalid: ' + $cycle) 'error'; return }

    Initialize-Log -Directory $Config.logDir | Out-Null
    Write-Log ('autonoma ' + (Get-AutonomaVersion()) + ' / target ' + $Config.targetStage)

    $state = Read-PlanState -PlanPath $Config.planPath
    if (-not (Test-Ledger -Config $Config)) { Write-Log 'ledger chain broken' 'error'; return }

    $ctx = @{
        socket = $null; stable = 0; lastTail = ''; lastGrowth = Get-Date; lastLength = 0
        lastTurn = Get-Date; pendingSince = Get-Date; paused = $false; lastNotice = Get-Date
        failures = 0; cardFailures = 0; editorSpin = 0; phases = $Phases; state = $state
    }

    if (-not (Get-DebugTargets -Port $Config.port)) {
        if (-not (Start-Browser -Config $Config)) { return }
        if (-not (Wait-DebugPort -Port $Config.port)) { Write-Log 'debug port dead' 'error'; return }
    }

    $required = [int][Math]::Ceiling((Get-Cfg $Config 'timing.quiet' 10) / [Math]::Max(1, (Get-Cfg $Config 'timing.poll' 3)))
    $poll = [Math]::Max(1, (Get-Cfg $Config 'timing.poll' 3))
    $patterns = $Config.siteAdapter.verdict

    while ($true) {
        $stop = Test-Budget -Config $Config -State $state
        if ($stop.Count -gt 0) { Write-Log ('budget stop: ' + ($stop -join ', ')) 'warn'; break }

        try {
            if (-not $ctx.socket -or $ctx.socket.State -ne [System.Net.WebSockets.WebSocketState]::Open) {
                $ctx.socket = Connect-Cdp -Port $Config.port -Url $Config.url
                if (-not $ctx.socket) { Start-Sleep -Seconds 3; continue }
                Write-Log 'attached'
                $ctx.stable = 0; $ctx.lastTail = ''
            }

            $page = Get-PageState -Socket $ctx.socket -Config $Config

            if ($page.captcha) {
                if (-not $ctx.paused) { $ctx.paused = $true; Write-Log 'CAPTCHA: solve it in the window, resuming automatically' 'warn'; Write-Beep 1200 600 }
            } elseif ($ctx.paused -and $page.hasEditor -and -not $page.editorText -and -not $page.busy) {
                $ctx.paused = $false; Write-Log 'captcha cleared'
            }
            if ($ctx.paused) {
                if (((Get-Date) - $ctx.lastNotice).TotalSeconds -ge 30) { $ctx.lastNotice = Get-Date; Write-Beep 1200 600 }
                Start-Sleep -Seconds $poll; continue
            }

            if ($page.card) {
                $maxCards = [int](Get-Cfg $Config 'limits.cards' 0)
                if ((Get-Cfg $Config 'seco' $false) -or ($maxCards -gt 0 -and $state.cards -ge $maxCards)) {
                    Write-Beep 700 400; Write-Log ('card held at ' + $state.cards) 'warn'
                    Start-Sleep -Seconds $poll; continue
                }
                $r = Invoke-CardClick -Socket $ctx.socket -Config $Config
                if ($r -like 'ok*') {
                    $state.cards++; $ctx.cardFailures = 0; $ctx.stable = 0
                    Write-Log ('card clicked ' + $state.cards + '/' + $maxCards)
                    Save-PlanState -PlanPath $Config.planPath -State $state
                } else {
                    $ctx.cardFailures++
                    if ($ctx.cardFailures -ge 3) { Invoke-Cdp -Socket $ctx.socket -Method 'Page.reload' | Out-Null; $ctx.cardFailures = 0; Start-Sleep -Seconds 8 }
                }
                Start-Sleep -Seconds 2; continue
            }

            if ($page.bodyLength -gt $ctx.lastLength) { $ctx.lastGrowth = Get-Date }
            $ctx.lastLength = $page.bodyLength
            $since = ((Get-Date) - $ctx.lastGrowth).TotalSeconds

            if ($page.busy -or $since -lt (Get-Cfg $Config 'timing.quiet' 10)) {
                $ctx.stable = 0; $ctx.lastTail = [string]$page.tail
                Start-Sleep -Seconds $poll; continue
            }

            if (-not $page.hasEditor) {
                $ctx.editorSpin++
                if ($ctx.editorSpin -ge 12) { Invoke-Cdp -Socket $ctx.socket -Method 'Page.reload' | Out-Null; $ctx.editorSpin = 0; Start-Sleep -Seconds 8; continue }
                Start-Sleep -Seconds $poll; continue
            }

            if ($page.editorText) {
                $ctx.editorSpin++
                if ($ctx.editorSpin -ge 4) {
                    Clear-Composer -Socket $ctx.socket -Config $Config | Out-Null
                    Start-Sleep -Milliseconds 400
                    if (Test-ComposerEmpty -Socket $ctx.socket -Config $Config) { $ctx.editorSpin = 0 } else { Start-Sleep -Seconds $poll; continue }
                } else { Start-Sleep -Seconds $poll; continue }
            } else { $ctx.editorSpin = 0 }

            if ($state.pending -and ((Get-Date) - $ctx.pendingSince).TotalMinutes -ge (Get-Cfg $Config 'timing.verdictForce' 5)) {
                $ctx.pendingSince = Get-Date; $state.lastHash = 'force-' + (Get-Random)
                Write-Log 'verdict stuck, forcing read' 'warn'
            }

            if (-not $state.pending -and ((Get-Date) - $ctx.lastTurn).TotalMinutes -ge (Get-Cfg $Config 'timing.watchdog' 8)) {
                $ctx.lastTurn = Get-Date; $ctx.stable = $required
                Write-Log 'watchdog push' 'warn'
            }

            if ([string]$page.tail -eq $ctx.lastTail) { $ctx.stable++ } else { $ctx.stable = 0; $ctx.lastTail = [string]$page.tail }
            if ($ctx.stable -lt $required) { Start-Sleep -Seconds $poll; continue }

            if ($state.pending) {
                $hash = Get-TextHash -Text ([string]$page.tail)
                if ($hash -eq $state.lastHash) { Start-Sleep -Seconds $poll; continue }
                $state.lastHash = $hash
                $offset = Get-VerdictOffset -BodyLength $page.bodyLength -TailLength ([string]$page.tail).Length
                $verdict = Read-Verdict -Text ([string]$page.tail) -Patterns $patterns -From $offset
                $ctx.pendingSince = Get-Date
                if (-not (Complete-Turn -Context $ctx -Config $Config -Verdict $verdict)) { break }
                Save-PlanState -PlanPath $Config.planPath -State $state
                $ctx.stable = 0; Start-Sleep -Seconds 2; continue
            }

            $item = Get-NextWorkItem -Context $ctx -Config $Config
            if (-not $item) { Write-Log 'plan exhausted'; break }
            if (-not (Send-WorkItem -Context $ctx -Config $Config -Item $item)) { Start-Sleep -Seconds 3; continue }
            Save-PlanState -PlanPath $Config.planPath -State $state
            Start-Sleep -Seconds 2
        } catch {
            Write-Log ('reconnect: ' + $_.Exception.Message) 'warn'
            if ($ctx.socket) { try { $ctx.socket.Dispose() } catch { } }
            $ctx.socket = $null
            Start-Sleep -Seconds 3
        }
    }

    if ($ctx.socket) { try { $ctx.socket.Dispose() } catch { } }
    Save-PlanState -PlanPath $Config.planPath -State $state
    Write-Log ('done. stage ' + $state.stage + ', turns ' + $state.turns + ', cards ' + $state.cards)
}

function Get-TextHash {
    param([string]$Text)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    return [BitConverter]::ToString([Security.Cryptography.SHA1]::Create().ComputeHash($bytes)).Substring(0, 8)
}

function Get-NextWorkItem {
    param([hashtable]$Context, [hashtable]$Config)

    $state = $Context.state
    $phase = Select-NextPhase -Phases $Context.phases -States $state.states -Passes $state.passes `
        -DeepRetries (Get-Cfg $Config 'limits.deepRetries' 8)

    if ($phase) {
        if ($state.states[$phase.id] -eq 'atascada') {
            $n = if ($state.passes.ContainsKey($phase.id)) { [int]$state.passes[$phase.id] } else { 0 }
            $state.passes[$phase.id] = $n + 1
            $state.parts[$phase.id] = 1
            $state.states[$phase.id] = 'curso'
        }
        return @{ kind = 'phase'; phase = $phase }
    }

    # No phase ready: run perfection rounds until the project is clean enough to advance.
    if ($state.stage -eq $Config.targetStage) { return $null }

    $clean = [int](Get-Cfg $Config 'limits.cleanAudits' 3)
    if ($state.clean -ge $clean) { return $null }
    if (-not $state.auditId) {
        $state.cycle++
        $state.auditId = 'A' + $state.cycle
        $state.states[$state.auditId] = 'curso'
        $state.parts[$state.auditId] = 1
    }
    $titles = @(Get-Cfg $Config 'audits' @())
    $title = if ($titles.Count -gt 0) { $titles[($state.cycle - 1) % $titles.Count] } else { 'sweep' }
    return @{ kind = 'audit'; id = $state.auditId; title = $title }
}

function Send-WorkItem {
    param([hashtable]$Context, [hashtable]$Config, [hashtable]$Item)

    $state = $Context.state
    $max = [int](Get-Cfg $Config 'limits.maxPromptChars' 9000)

    if ($Item.kind -eq 'audit') {
        $text = New-AuditPrompt -Cycle $state.cycle -Title $Item.title `
            -Brief 'Sweep the project for whatever breaks the result. Fix each finding with its own commit, run the gates, record the counts.' `
            -Footer (New-Contract -Criterion 'no findings and gates green')
        $chunks = Split-Prompt -Text $text -Max $max
        return (Send-Chunks -Context $Context -Config $Config -Chunks $chunks -Pending ($Item.id + '|1') -Part 1 -Total 0)
    }

    $phase = $Item.phase
    $total = Get-PartCount -PlanPath $Config.planPath -PhaseId $phase.id
    $part = if ($state.parts.ContainsKey($phase.id)) { [int]$state.parts[$phase.id] } else { 1 }
    if ($total -gt 0 -and $part -gt ($total + 1)) { $part = $total + 1 }

    $body = if ($total -gt 0 -and $part -le $total) { Get-BriefBody -PlanPath $Config.planPath -PhaseId $phase.id -Index $part } else { '' }
    $pass = if ($state.passes.ContainsKey($phase.id)) { [int]$state.passes[$phase.id] } else { 0 }
    $footer = New-Contract -Criterion $phase.criterion

    $text = New-PhasePrompt -Phase $phase -Part $part -Total $total -Body $body -Pass $pass `
        -DeepRetries (Get-Cfg $Config 'limits.deepRetries' 8) `
        -ContractHeader ([bool](Get-Cfg $Config 'contract.header' $true)) -ContractFooter $footer

    $chunks = Split-Prompt -Text $text -Max $max
    return (Send-Chunks -Context $Context -Config $Config -Chunks $chunks -Pending ($phase.id + '|' + $part) -Part $part -Total $total)
}

function Send-Chunks {
    param([hashtable]$Context, [hashtable]$Config, [string[]]$Chunks, [string]$Pending, [int]$Part, [int]$Total)

    for ($i = 0; $i -lt $Chunks.Count; $i++) {
        $chunk = if ($Chunks.Count -gt 1) { '[' + ($i + 1) + '/' + $Chunks.Count + '] ' + $Chunks[$i] } else { $Chunks[$i] }
        $r = Send-Message -Socket $Context.socket -Config $Config -Text $chunk
        if ($r -notlike 'ok*') {
            $Context.failures++
            Write-Log ('send failed: ' + $r + ' (' + $Context.failures + '/5)') 'warn'
            if ($Context.failures -ge 5) { if ($Context.socket) { $Context.socket.Dispose() }; $Context.socket = $null; $Context.failures = 0 }
            return $false
        }
        if ($i -lt ($Chunks.Count - 1)) { Start-Sleep -Milliseconds (Get-Cfg $Config 'limits.chunkDelay' 1200) }
    }

    $state = $Context.state
    $state.turns++
    # Anchor the rollback target before the first piece of work goes out, so a
    # later regression can always undo the run's own changes.
    if (-not $state.startSha) {
        Push-Location $Config.rootDir
        try { $sha = (git rev-parse HEAD 2>$null); if ($LASTEXITCODE -eq 0) { $state.startSha = $sha } } catch { } finally { Pop-Location }
    }
    $state.pending = $Pending
    $state.lastHash = ''
    $Context.pendingSince = Get-Date
    $Context.lastTurn = Get-Date
    $Context.failures = 0
    $Context.stable = 0
    Write-Log ('turn ' + $state.turns + ' -> ' + $Pending)
    return $true
}

function Complete-Turn {
    param([hashtable]$Context, [hashtable]$Config, [hashtable]$Verdict)

    $state = $Context.state
    $parts = $state.pending.Split('|')
    $id = $parts[0]
    $part = if ($parts.Count -gt 1) { [int]$parts[1] } else { 1 }
    $total = if ($id -like 'A*') { 0 } else { Get-PartCount -PlanPath $Config.planPath -PhaseId $id }

    if ($total -gt 0 -and $part -le $total) {
        if ($Verdict.state -in @('rojo', 'bloqueado')) {
            $state.parts[$id] = 1; $state.states[$id] = 'rojo1'
            Write-Log ('RED ' + $id + ' part ' + $part + ': ' + $Verdict.evidence) 'warn'
        } else {
            $state.parts[$id] = $part + 1; $state.states[$id] = 'curso'
            Write-Log ('part ' + $part + '/' + $total + ' of ' + $id + ' done')
        }
        $state.pending = ''
        return $true
    }

    $claim = Test-Acceptance -Verdict $Verdict -RequireEvidence ([bool](Get-Cfg $Config 'requireEvidence' $true))
    if ($claim -eq 'bloqueado') { Park-Phase -Context $Context -Id $id -Reason ('blocked: ' + $Verdict.evidence); return $true }

    if ($claim -eq 'verde') {
        $axis = if ($id -like 'A*') { '' } else { ($Context.phases | Where-Object { $_.id -eq $id }).axis }
        $set = Invoke-GateSet -Config $Config -PhaseId $id -Axis $axis
        foreach ($g in $set.results) {
            Add-LedgerEntry -Config $Config -Entry @{
                kind = 'gate'; phase = $id; gate = $g.id; axis = $g.axis
                status = $g.status; severity = $g.severity; weight = $g.weight; sha256 = $g.sha256
            }
        }

        if ($set.blocked) {
            Write-Log ('RED ' + $id + ': blocker gate failed') 'warn'
            Add-Telemetry -Config $Config -Entry @{ phase = $id; verdict = 'rojo'; cause = 'gate' }
            if ([bool](Get-Cfg $Config 'limits.rollbackOnRegression' $true)) { Invoke-Rollback -Config $Config -Sha $state.startSha }
            $state.regressions++
            $state.pending = ''; $state.states[$id] = 'rojo1'; $state.parts[$id] = 1
            return $true
        }

        $before = Get-Scorecard -Results (Get-LedgerResults -Config $Config -Phase $id) -Weights $Config.weights
        $after = Get-Scorecard -Results (Get-LedgerResults -Config $Config -Phase '*') -Weights $Config.weights
        if ($after.global -lt ($before.global - $Config.tolerance) -and $before.global -gt 0) {
            Write-Log ('RED ' + $id + ': score regression ' + $before.global + ' -> ' + $after.global) 'warn'
            Add-Telemetry -Config $Config -Entry @{ phase = $id; verdict = 'rojo'; cause = 'regression' }
            if ([bool](Get-Cfg $Config 'limits.rollbackOnRegression' $true)) { Invoke-Rollback -Config $Config -Sha $state.startSha }
            $state.regressions++
            $state.pending = ''; $state.states[$id] = 'rojo1'; $state.parts[$id] = 1
            return $true
        }

        $state.regressions = 0
        $state.states[$id] = 'verde'
        $state.pending = ''
        Add-LedgerEntry -Config $Config -Entry @{ kind = 'phase'; phase = $id; verdict = 'verde'; score = $after.global }
        Add-Telemetry -Config $Config -Entry @{ phase = $id; verdict = 'verde'; score = $after.global }

        if ($id -like 'A*') {
            if ($Verdict.clean) { $state.clean++ } else { $state.clean = 0 }
            $state.auditId = ''
        }

        $next = Get-NextStage -Config $Config -Stage $state.stage
        if ($next) {
            $floors = ConvertTo-Hashtable $Config.stages.floors[$next]
            if ($floors) {
                $missing = Test-Floors -Card $after -Floors $floors
                if ($missing.Count -eq 0) {
                    $state.stage = $next
                    Add-LedgerEntry -Config $Config -Entry @{ kind = 'stage'; stage = $next; score = $after.global }
                    Write-Log ('STAGE ADVANCE -> ' + $next)
                }
            }
        }
        return $true
    }

    $reds = if ($state.states[$id] -like 'rojo*') { [int]($state.states[$id] -replace '\D', '') } else { 0 }
    $reds++
    $state.pending = ''
    Add-Telemetry -Config $Config -Entry @{ phase = $id; verdict = 'rojo'; cause = 'claim' }
    if ($id -like 'A*') { $state.clean = 0; $state.auditId = '' }
    elseif ($reds -ge (Get-Cfg $Config 'limits.redsPerPhase' 3)) { Park-Phase -Context $Context -Id $id -Reason ($reds.ToString() + ' reds') }
    else {
        $state.states[$id] = 'rojo' + $reds
        $state.parts[$id] = 1
        Write-Log ('RED ' + $id + ' ' + $reds + '/3: ' + $Verdict.state) 'warn'
    }
    return $true
}

function Invoke-Rollback {
    param([hashtable]$Config, [string]$Sha)

    if (-not $Sha) { return }
    Push-Location $Config.rootDir
    try {
        git reset --hard $Sha 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Log ('rolled back to ' + $Sha) }
    } catch { Write-Log 'rollback failed' 'warn' } finally { Pop-Location }
}

function Park-Phase {
    param([hashtable]$Context, [string]$Id, [string]$Reason)

    $Context.state.states[$Id] = 'atascada'
    $Context.state.parts[$Id] = 1
    $Context.state.pending = ''
    Write-Log ('PARKED ' + $Id + ': ' + $Reason) 'error'
}
