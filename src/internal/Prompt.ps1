function New-Contract {
    param([string]$Criterion)

    $lines = @(
        'EXIT CONTRACT (mandatory, on its own lines, at the end):',
        'ESTADO: verde | rojo | bloqueado',
        'EVIDENCIA: paths and real command output',
        'GATES: ids of the gates you made pass'
    )
    if ($Criterion) { $lines += ('ACCEPTANCE: ' + $Criterion) }
    return ($lines -join "`n")
}

function Split-Prompt {
    param([Parameter(Mandatory)][string]$Text, [int]$Max = 9000)

    if ($Text.Length -le $Max) { return @($Text) }

    # Split on line boundaries so a chunk never cuts a line, and long lines are
    # hard-split only as a last resort. The contract is repeated by the caller,
    # so truncation cannot eat it.
    $chunks = New-Object System.Collections.Generic.List[string]
    $buffer = New-Object Text.StringBuilder

    foreach ($line in ($Text -split "`r?`n")) {
        if (($buffer.Length + $line.Length + 1) -gt $Max -and $buffer.Length -gt 0) {
            $chunks.Add($buffer.ToString().Trim()); [void]$buffer.Clear()
        }
        if ($line.Length -gt $Max) {
            $rest = $line
            while ($rest.Length -gt $Max) { $chunks.Add($rest.Substring(0, $Max)); $rest = $rest.Substring($Max) }
            [void]$buffer.AppendLine($rest); continue
        }
        [void]$buffer.AppendLine($line)
    }
    if ($buffer.Length -gt 0) { $chunks.Add($buffer.ToString().Trim()) }
    return $chunks.ToArray()
}

function New-PhasePrompt {
    param(
        [Parameter(Mandatory)]$Phase,
        [int]$Part, [int]$Total, [string]$Body, [int]$Pass, [int]$DeepRetries,
        [bool]$ContractHeader, [string]$ContractFooter
    )

    $head = 'PHASE ' + $Phase.id + ' [' + $Phase.axis + '] ' + $Phase.title + '.'
    if ($Total -gt 0) { $head += ' PART ' + $Part + ' of ' + $Total + '.' }
    if ($Pass -gt 0) { $head += ' DEEP RETRY ' + $Pass + '/' + $DeepRetries + ': change approach, record why the last one failed in DECISIONS.md.' }

    $closing = ($Total -eq 0 -or $Part -gt $Total)
    $text = $head
    if ($ContractHeader -and $closing) { $text += "`n`n" + $ContractFooter }
    if ($Body) { $text += "`n`n" + $Body }
    if ($closing) { $text += "`n`n" + $ContractFooter }
    else { $text += "`n`nClose with one line: HECHO: what you did and where." }
    return $text
}

function New-AuditPrompt {
    param([int]$Cycle, [string]$Title, [string]$Brief, [string]$Footer)

    return ('PERFECTION ROUND ' + $Cycle + ' (' + $Title + '). ' + $Brief + "`n`n" +
        'If nothing is left, write a lone line SIN HALLAZGOS.' + "`n`n" + $Footer)
}
