$script:JsCache = @{}

function Get-PageScript {
    param([Parameter(Mandatory)][string]$Name)

    if ($script:JsCache.ContainsKey($Name)) { return $script:JsCache[$Name] }
    $path = Join-Path (Join-Path $script:JsDir) ($Name + '.js')
    $script:JsCache[$Name] = Get-Content -Raw -Encoding UTF8 -LiteralPath $path
    return $script:JsCache[$Name]
}

function Invoke-Page {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][string]$Name, [hashtable]$Params = @{})

    $payload = $Params | ConvertTo-Json -Depth 6 -Compress
    return Invoke-Eval -Socket $Socket -Expression ('(function(p){' + (Get-PageScript -Name $Name) + '})(' + $payload + ')')
}

function Get-AdapterParams {
    param([hashtable]$Config)

    $a = $Config.siteAdapter
    return @{
        editor = $a.editor; sendButton = $a.sendButton; busyButton = $a.busyButton
        busyText = @($a.busyText); continueCard = $a.continueCard
        continueText = @($a.continueText); captcha = @($a.captcha)
    }
}

function Get-PageState {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][hashtable]$Config)
    return Invoke-Page -Socket $Socket -Name 'state' -Params (Get-AdapterParams -Config $Config)
}

function Set-ComposerFocus {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][hashtable]$Config)
    return [bool](Invoke-Page -Socket $Socket -Name 'composer' -Params @{ editor = $Config.siteAdapter.editor; action = 'focus' })
}

function Clear-Composer {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][hashtable]$Config)
    return [bool](Invoke-Page -Socket $Socket -Name 'composer' -Params @{ editor = $Config.siteAdapter.editor; action = 'clear' })
}

function Test-ComposerEmpty {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][hashtable]$Config)
    return [bool](Invoke-Page -Socket $Socket -Name 'composer' -Params @{ editor = $Config.siteAdapter.editor; action = 'empty' })
}

function Test-CardPresent {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][hashtable]$Config)
    $p = Get-AdapterParams -Config $Config; $p.action = 'detect'
    return [bool](Invoke-Page -Socket $Socket -Name 'card' -Params $p)
}

function Send-Enter {
    param([Parameter(Mandatory)]$Socket)

    $key = @{ key = 'Enter'; code = 'Enter'; windowsVirtualKeyCode = 13; nativeVirtualKeyCode = 13 }
    Invoke-Cdp -Socket $Socket -Method 'Input.dispatchKeyEvent' -Params (@{ type = 'rawKeyDown' } + $key) | Out-Null
    Invoke-Cdp -Socket $Socket -Method 'Input.dispatchKeyEvent' -Params (@{ type = 'char'; text = "`r"; unmodifiedText = "`r" } + $key) | Out-Null
    Invoke-Cdp -Socket $Socket -Method 'Input.dispatchKeyEvent' -Params (@{ type = 'keyUp' } + $key) | Out-Null
}

function Invoke-CardClick {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][hashtable]$Config)

    # Try a real mouse click on the card first; fall back to focusing it and pressing Enter.
    $p = Get-AdapterParams -Config $Config; $p.action = 'click'
    $pos = Invoke-Page -Socket $Socket -Name 'card' -Params $p
    if ($pos) {
        Start-Sleep -Milliseconds 500
        foreach ($type in @('mousePressed', 'mouseReleased')) {
            Invoke-Cdp -Socket $Socket -Method 'Input.dispatchMouseEvent' -Params @{
                type = $type; x = [int]$pos.x; y = [int]$pos.y; button = 'left'; clickCount = 1
            } | Out-Null
        }
        Start-Sleep -Milliseconds 600
        if (-not (Test-CardPresent -Socket $Socket -Config $Config)) { return 'ok' }
    }

    $p.action = 'focus'
    if (Invoke-Page -Socket $Socket -Name 'card' -Params $p) {
        Send-Enter -Socket $Socket
        Start-Sleep -Milliseconds 600
        if (-not (Test-CardPresent -Socket $Socket -Config $Config)) { return 'ok-key' }
    }
    return 'fail'
}

function Send-Message {
    param([Parameter(Mandatory)]$Socket, [Parameter(Mandatory)][hashtable]$Config, [Parameter(Mandatory)][string]$Text)

    if (-not (Set-ComposerFocus -Socket $Socket -Config $Config)) { return 'no-editor' }
    Start-Sleep -Milliseconds 150
    if (-not (Test-ComposerEmpty -Socket $Socket -Config $Config)) {
        Clear-Composer -Socket $Socket -Config $Config | Out-Null
        Start-Sleep -Milliseconds 200
    }

    Invoke-Cdp -Socket $Socket -Method 'Input.insertText' -Params @{ text = $Text } | Out-Null
    Start-Sleep -Milliseconds 450

    # Some editors swallow the first insert; clear and retry once if so.
    if (Test-ComposerEmpty -Socket $Socket -Config $Config) {
        Set-ComposerFocus -Socket $Socket -Config $Config | Out-Null
        Clear-Composer -Socket $Socket -Config $Config | Out-Null
        Invoke-Cdp -Socket $Socket -Method 'Input.insertText' -Params @{ text = $Text } | Out-Null
        Start-Sleep -Milliseconds 550
        if (Test-ComposerEmpty -Socket $Socket -Config $Config) { return 'not-inserted' }
    }

    Send-Enter -Socket $Socket
    Start-Sleep -Milliseconds (Get-Cfg $Config 'timing.sendSettle' 1600)
    if (Test-ComposerEmpty -Socket $Socket -Config $Config) { return 'ok' }

    # Enter did not submit: press the send button directly.
    $clicked = Invoke-Page -Socket $Socket -Name 'composer' -Params @{ editor = $Config.siteAdapter.sendButton; action = 'click' }
    if ($clicked) {
        Start-Sleep -Milliseconds 1200
        if (Test-ComposerEmpty -Socket $Socket -Config $Config) { return 'ok-button' }
    }
    return 'not-sent'
}
