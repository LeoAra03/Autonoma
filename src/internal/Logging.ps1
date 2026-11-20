$script:LogPath = $null
$script:LogMax = 4MB
$script:LogRank = @{ debug = 10; info = 20; warn = 30; error = 40 }
$script:LogLevel = 20
$script:BeepEnabled = $true

function Initialize-Log {
    param([Parameter(Mandatory)][string]$Directory)

    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
    $script:LogPath = Join-Path $Directory ('autonoma-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Set-Content -LiteralPath $script:LogPath -Value '' -Encoding UTF8
    return $script:LogPath
}

function Write-Log {
    param(
        [Parameter(Mandatory)][string]$Message,
        [ValidateSet('debug', 'info', 'warn', 'error')][string]$Level = 'info'
    )

    if ($script:LogRank[$Level] -lt $script:LogLevel) { return }
    $line = '[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $Level.ToUpperInvariant().PadRight(5) + ' ' + $Message

    switch ($Level) {
        'error' { Write-Host $line -ForegroundColor Red }
        'warn'  { Write-Host $line -ForegroundColor Yellow }
        'debug' { Write-Host $line -ForegroundColor DarkGray }
        default { Write-Host $line }
    }

    if (-not $script:LogPath) { return }
    try {
        if ((Get-Item -LiteralPath $script:LogPath).Length -gt $script:LogMax) {
            Move-Item -LiteralPath $script:LogPath -Destination ($script:LogPath -replace '\.log$', '.1.log') -Force
            Set-Content -LiteralPath $script:LogPath -Value '' -Encoding UTF8
        }
        Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
    } catch { }
}

function Write-Beep {
    param([int]$Frequency = 880, [int]$Milliseconds = 350)
    if (-not $script:BeepEnabled) { return }
    try { [Console]::Beep($Frequency, $Milliseconds) } catch { }
}
