function Get-LedgerPath {
    param([hashtable]$Config)
    return (Join-Path $Config.stateDir 'ledger.jsonl')
}

function Add-LedgerEntry {
    param([Parameter(Mandatory)][hashtable]$Config, [Parameter(Mandatory)][hashtable]$Entry)

    $path = Get-LedgerPath -Config $Config
    $previous = ''
    if (Test-Path -LiteralPath $path) {
        $last = Get-Content -LiteralPath $path -Tail 1 -Encoding UTF8
        if ($last) { $previous = ($last | ConvertFrom-Json).hash }
    }

    $Entry.at = (Get-Date).ToString('s')
    $Entry.prev = $previous
    $payload = $Entry | ConvertTo-Json -Depth 6 -Compress

    # The hash covers the entry minus the hash field, so the chain verifies
    # without having to strip anything before re-hashing.
    $stream = New-Object IO.MemoryStream
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Position = 0
    $Entry.hash = (Get-FileHash -InputStream $stream -Algorithm SHA256).Hash
    $stream.Dispose()

    Add-Content -LiteralPath $path -Value ($Entry | ConvertTo-Json -Depth 6 -Compress) -Encoding UTF8
}

function Test-Ledger {
    param([Parameter(Mandatory)][hashtable]$Config)

    $path = Get-LedgerPath -Config $Config
    if (-not (Test-Path -LiteralPath $path)) { return $true }

    $previous = ''
    foreach ($line in (Get-Content -LiteralPath $path -Encoding UTF8)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $entry = $line | ConvertFrom-Json
        if ($entry.prev -ne $previous) { return $false }
        $previous = $entry.hash
    }
    return $true
}

function Get-LedgerResults {
    param([hashtable]$Config, [string]$Phase = '*')

    $path = Get-LedgerPath -Config $Config
    if (-not (Test-Path -LiteralPath $path)) { return @() }

    $all = New-Object System.Collections.Generic.List[object]
    foreach ($line in (Get-Content -LiteralPath $path -Encoding UTF8)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $entry = $line | ConvertFrom-Json
        if ($entry.kind -ne 'gate') { continue }
        if ($Phase -ne '*' -and $entry.phase -ne $Phase) { continue }
        $all.Add($entry)
    }
    return $all.ToArray()
}
