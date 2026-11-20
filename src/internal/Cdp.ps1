$script:CdpId = 0

function Invoke-Cdp {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][string]$Method, $Params = @{}, [int]$TimeoutMs = 20000)

    $script:CdpId++
    $myId = $script:CdpId
    $json = @{ id = $myId; method = $Method; params = $Params } | ConvertTo-Json -Depth 12 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $segment = New-Object 'System.ArraySegment[byte]' -ArgumentList @(, $bytes)

    try {
        $task = $Socket.SendAsync($segment, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, [Threading.CancellationToken]::None)
        if (-not $task.Wait($TimeoutMs)) { throw (New-Object TimeoutException ('send ' + $Method)) }
    } catch [System.AggregateException] { throw $_.Exception.InnerException }

    # Drain frames until the reply with our id arrives; CDP events may interleave.
    $buffer = New-Object byte[] 65536
    $builder = New-Object Text.StringBuilder
    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)

    while ($true) {
        [void]$builder.Clear()
        $complete = $false
        while (-not $complete) {
            $remaining = [int][Math]::Max(1, ($deadline - (Get-Date)).TotalMilliseconds)
            $receive = New-Object 'System.ArraySegment[byte]' -ArgumentList @(, $buffer)
            try {
                $read = $Socket.ReceiveAsync($receive, [Threading.CancellationToken]::None)
                if (-not $read.Wait($remaining)) { throw (New-Object TimeoutException ('read ' + $Method)) }
            } catch [System.AggregateException] { throw $_.Exception.InnerException }
            $frame = $read.Result
            [void]$builder.Append([Text.Encoding]::UTF8.GetString($buffer, 0, $frame.Count))
            $complete = $frame.EndOfMessage
        }
        # Convert to a hashtable so interleaved CDP events (which carry no id)
        # cannot trip strict mode on a missing property.
        $message = ConvertTo-Hashtable ($builder.ToString() | ConvertFrom-Json)
        if ($message['id'] -eq $myId) { return $message }
    }
}

function Invoke-Eval {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][string]$Expression)

    $response = Invoke-Cdp -Socket $Socket -Method 'Runtime.evaluate' -Params @{
        expression = $Expression; returnByValue = $true; awaitPromise = $true; userGesture = $true
    }
    # A hashtable view: CDP success responses carry no "error" key at all.
    $r = ConvertTo-Hashtable $response
    if ($r['error']) { throw $r['error'].message }
    if ($r['result'].exceptionDetails) { throw $r['result'].exceptionDetails.text }
    return $r['result'].result.value
}

function Get-DebugTargets {
    param([int]$Port)
    try { return (Invoke-RestMethod -Uri ('http://127.0.0.1:' + $Port + '/json/list') -TimeoutSec 5) } catch { return $null }
}

function Find-ChatTab {
    param([int]$Port, [string]$Url)

    $targets = Get-DebugTargets -Port $Port
    if (-not $targets) { return $null }
    $pages = @($targets | Where-Object { $_.type -eq 'page' -and $_.webSocketDebuggerUrl -and $_.url -notlike 'devtools://*' })
    if ($pages.Count -eq 0) { return $null }
    $host_ = ([Uri]$Url).Host
    $hit = $pages | Where-Object { $_.url -like ('*' + $host_ + '*') } | Select-Object -First 1
    if ($hit) { return $hit }
    return ($pages | Select-Object -First 1)
}

function Open-ChatTab {
    param([int]$Port, [string]$Url)

    $uri = 'http://127.0.0.1:' + $Port + '/json/new?url=' + [Uri]::EscapeDataString($Url)
    foreach ($m in @('Put', 'Get')) {
        try { $tab = Invoke-RestMethod -Method $m -Uri $uri -TimeoutSec 10; if ($tab.webSocketDebuggerUrl) { return $tab } } catch { }
    }
    return $null
}

function Connect-Cdp {
    param([int]$Port, [string]$Url)

    $tab = Find-ChatTab -Port $Port -Url $Url
    if (-not $tab) { $tab = Open-ChatTab -Port $Port -Url $Url; Start-Sleep -Seconds 4; if (-not $tab) { return $null } }

    $socket = New-Object System.Net.WebSockets.ClientWebSocket
    $socket.Options.KeepAliveInterval = [TimeSpan]::FromSeconds(10)
    try { $socket.ConnectAsync([Uri]$tab.webSocketDebuggerUrl, [Threading.CancellationToken]::None).Wait(15000) | Out-Null }
    catch { $socket.Dispose(); return $null }
    if ($socket.State -ne [System.Net.WebSockets.WebSocketState]::Open) { $socket.Dispose(); return $null }
    return $socket
}
