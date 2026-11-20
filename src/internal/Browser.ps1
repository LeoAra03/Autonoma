$script:Browsers = @(
    @{ name = 'chrome'; path = 'Google\Chrome\Application\chrome.exe' },
    @{ name = 'edge'; path = 'Microsoft\Edge\Application\msedge.exe' },
    @{ name = 'brave'; path = 'BraveSoftware\Brave-Browser\Application\brave.exe' },
    @{ name = 'chromium'; path = 'Chromium\Application\chrome.exe' },
    @{ name = 'thorium'; path = 'Thorium\Application\thorium.exe' }
)

function Find-Browser {
    param([string]$Channel = 'auto')

    foreach ($base in @($env:PROGRAMFILES, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if (-not $base) { continue }
        foreach ($b in $script:Browsers) {
            if ($Channel -ne 'auto' -and $b.name -ne $Channel) { continue }
            $full = Join-Path $base $b.path
            if (Test-Path -LiteralPath $full) { return $full }
        }
    }
    return $null
}

function Start-Browser {
    param([hashtable]$Config)

    $exe = Find-Browser -Channel (Get-Cfg $Config 'browser.channel' 'auto')
    if (-not $exe) { Write-Log 'no chromium browser found; pass -Browser <path>' 'error'; return $false }

    $browserArgs = @(
        '--remote-debugging-port=' + $Config.port, '--remote-allow-origins=*',
        '--no-first-run', '--no-default-browser-check', '--user-data-dir=' + $Config.browser.profile
    )
    if (Get-Cfg $Config 'browser.headless' $false) { $browserArgs += '--headless=new' }
    foreach ($extra in @(Get-Cfg $Config 'browser.args' @())) { if ($extra) { $browserArgs += $extra } }
    $browserArgs += $Config.url

    Write-Log ('launching ' + (Split-Path -Leaf $exe) + ' on port ' + $Config.port)
    Start-Process -FilePath $exe -ArgumentList $browserArgs
    return $true
}

function Wait-DebugPort {
    param([int]$Port, [int]$TimeoutSeconds = 60)

    $waited = 0
    while ($waited -lt $TimeoutSeconds) {
        if (Get-DebugTargets -Port $Port) { return $true }
        Start-Sleep -Seconds 2
        $waited += 2
    }
    return $false
}
