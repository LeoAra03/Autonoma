function Get-PhaseState {
    param([hashtable]$States, [string]$Id)
    if ($States.ContainsKey($Id)) { return [string]$States[$Id] }
    return 'pendiente'
}

function ConvertTo-GlobRegex {
    param([Parameter(Mandatory)][string]$Glob)

    $pattern = [regex]::Escape($Glob)
    $pattern = $pattern -replace '\\*\\*', '§DS§'
    $pattern = $pattern -replace '\\\*', '[^/\\]*'
    $pattern = $pattern -replace '§DS§/', '(.*/)?'
    $pattern = $pattern -replace '§DS§', '.*'
    $pattern = $pattern -replace '\\\?', '.'
    $pattern = $pattern -replace '\\{([^\\]*)\\}', '($1)'
    return ('^' + $pattern + '$')
}

function Test-Glob {
    param([string]$Glob, [string]$Path)

    $normalized = $Path -replace '\\', '/'
    return [regex]::IsMatch($normalized, (ConvertTo-GlobRegex -Glob $Glob), 'IgnoreCase')
}

function Get-ReadySet {
    param([object[]]$Phases, [hashtable]$States, [hashtable]$Lanes = @{})

    return @($Phases | Where-Object {
            (Get-PhaseState -States $States -Id $_.id) -eq 'pendiente' -and
            @($_.depends | Where-Object { (Get-PhaseState -States $States -Id $_) -ne 'verde' }).Count -eq 0 -and
            -not ($Lanes.ContainsKey($_.axis) -and $Lanes[$_.axis] -eq $true)
        })
}

function Select-NextPhase {
    param([object[]]$Phases, [hashtable]$States, [hashtable]$Passes, [int]$DeepRetries = 8)

    # A phase in progress wins, then the most recent red, then anything ready.
    foreach ($p in $Phases) { if ((Get-PhaseState $States $p.id) -eq 'curso') { return $p } }
    foreach ($p in $Phases) { if ((Get-PhaseState $States $p.id) -like 'rojo*') { return $p } }

    foreach ($p in (Get-ReadySet -Phases $Phases -States $States)) { return $p }

    # Phases parked as atascada get a bounded number of deep retries before they stay parked.
    foreach ($p in $Phases) {
        if ((Get-PhaseState $States $p.id) -ne 'atascada') { continue }
        $n = if ($Passes.ContainsKey($p.id)) { [int]$Passes[$p.id] } else { 0 }
        if ($n -lt $DeepRetries) { return $p }
    }
    return $null
}

function Test-GraphCycles {
    param([object[]]$Phases)

    $ids = @($Phases | ForEach-Object { $_.id })
    foreach ($p in $Phases) {
        foreach ($dep in $p.depends) {
            if ($ids -notcontains $dep) { return ('unknown dependency ' + $dep + ' in ' + $p.id) }
        }
    }

    # Classic white/grey/black depth-first cycle detection.
    $colour = @{}
    foreach ($id in $ids) { $colour[$id] = 0 }
    $visit = {
        param($id)
        $colour[$id] = 1
        $node = $Phases | Where-Object { $_.id -eq $id } | Select-Object -First 1
        foreach ($dep in $node.depends) {
            if ($colour[$dep] -eq 1) { return ('cycle at ' + $dep) }
            if ($colour[$dep] -eq 0) { $r = & $visit $dep; if ($r) { return $r } }
        }
        $colour[$id] = 2
        return $null
    }
    foreach ($id in $ids) { if ($colour[$id] -eq 0) { $r = & $visit $id; if ($r) { return $r } } }
    return $null
}
