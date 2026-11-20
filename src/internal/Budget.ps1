function Test-Budget {
    param([hashtable]$Config, [hashtable]$State)

    # Returns the reasons the run should stop, empty when the run is still healthy.
    $reasons = New-Object System.Collections.Generic.List[string]

    $turns = [int](Get-Cfg $Config 'limits.turns' 0)
    if ($turns -gt 0 -and $State.turns -ge $turns) { $reasons.Add('turns') }

    $wall = [int](Get-Cfg $Config 'limits.wallMinutes' 0)
    if ($wall -gt 0 -and ((Get-Date) - [datetime]$State.startedAt).TotalMinutes -ge $wall) { $reasons.Add('wall clock') }

    $reg = [int](Get-Cfg $Config 'limits.maxRegressions' 0)
    if ($reg -gt 0 -and $State.regressions -ge $reg) { $reasons.Add('regressions') }

    return $reasons.ToArray()
}
