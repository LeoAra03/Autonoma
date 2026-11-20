function Read-Verdict {
    param([string]$Text, [hashtable]$Patterns, [int]$From = 0)

    $result = @{ state = ''; evidence = ''; gates = ''; done = ''; clean = $false }
    if ([string]::IsNullOrEmpty($Text)) { return $result }

    $segment = if ($From -gt 0 -and $From -lt $Text.Length) { $Text.Substring($From) } else { $Text }

    foreach ($key in @('state', 'evidence', 'gates', 'done', 'clean')) {
        $pattern = $Patterns[$key]
        if ([string]::IsNullOrEmpty($pattern)) { continue }
        $m = [regex]::Matches($segment, $pattern, 'IgnoreCase, Multiline')
        if ($m.Count -eq 0) { continue }
        $last = $m[$m.Count - 1]
        if ($key -eq 'clean') { $result.clean = $true; continue }
        $value = if ($last.Groups.Count -gt 1) { $last.Groups[1].Value.Trim() } else { '' }
        if ($key -eq 'state') { $value = $value.ToLowerInvariant() }
        $result[$key] = $value
    }
    return $result
}

function Get-VerdictOffset {
    param([int]$BodyLength, [int]$TailLength)
    return [Math]::Max(0, $BodyLength - $TailLength)
}

function Test-Acceptance {
    param([hashtable]$Verdict, [bool]$RequireEvidence = $true)

    if ($Verdict.state -eq 'bloqueado') { return 'bloqueado' }
    if ($Verdict.state -eq 'verde') {
        if ($RequireEvidence -and [string]::IsNullOrWhiteSpace($Verdict.evidence)) { return 'rojo' }
        return 'verde'
    }
    return 'rojo'
}
