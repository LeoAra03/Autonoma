function Get-AxisScore {
    param([object[]]$Results)

    $total = 0
    $passed = 0
    foreach ($r in $Results) {
        # Skipped gates count toward neither the numerator nor the denominator:
        # a missing tool lowers coverage instead of being claimed as a pass.
        if ($r.status -eq 'skipped') { continue }
        $total += [int]$r.weight
        if ($r.status -eq 'pass') { $passed += [int]$r.weight }
    }
    if ($total -eq 0) { return $null }
    return [Math]::Round(100 * $passed / $total, 1)
}

function Get-Scorecard {
    param([object[]]$Results, [hashtable]$Weights)

    $axes = @{}
    foreach ($group in ($Results | Group-Object axis)) { $axes[$group.Name] = Get-AxisScore -Results $group.Group }

    $global = 0
    foreach ($axis in $Weights.Keys) {
        $value = if ($axes.ContainsKey($axis) -and $null -ne $axes[$axis]) { [double]$axes[$axis] } else { 0 }
        $global += $value * [double]$Weights[$axis] / 100
    }

    return [pscustomobject]@{
        at     = (Get-Date).ToString('s')
        axes   = $axes
        global = [Math]::Round($global, 1)
        gaps   = @($Weights.Keys | Where-Object { -not $axes.ContainsKey($_) -or $null -eq $axes[$_] })
    }
}

function Test-Floors {
    param([pscustomobject]$Card, [hashtable]$Floors)

    $missing = New-Object System.Collections.Generic.List[string]
    foreach ($axis in $Floors.Keys) {
        # An axis with no measurements counts as 0: unmeasured is not exempt.
        $value = if ($Card.axes.ContainsKey($axis) -and $null -ne $Card.axes[$axis]) { [double]$Card.axes[$axis] } else { 0 }
        if ($value -lt [double]$Floors[$axis]) { $missing.Add(($axis + ' ' + $value + '<' + $Floors[$axis])) }
    }
    return $missing.ToArray()
}

function Compare-Scorecard {
    param([pscustomobject]$Before, [pscustomobject]$After)

    $deltas = @{}
    foreach ($axis in $After.axes.Keys) {
        $old = if ($Before.axes.ContainsKey($axis) -and $null -ne $Before.axes[$axis]) { [double]$Before.axes[$axis] } else { 0 }
        $deltas[$axis] = [Math]::Round([double]$After.axes[$axis] - $old, 1)
    }
    return [pscustomobject]@{
        global    = [Math]::Round($After.global - $Before.global, 1)
        axes      = $deltas
        regressed = @($deltas.Keys | Where-Object { $deltas[$_] -lt 0 })
    }
}

function Get-NextStage {
    param([hashtable]$Config, [string]$Stage)

    $order = @($Config.stages.order)
    $index = [Array]::IndexOf($order, $Stage)
    if ($index -lt 0 -or $index -ge ($order.Count - 1)) { return $null }
    return $order[$index + 1]
}
