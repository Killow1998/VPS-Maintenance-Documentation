# Public bootstrap only. The client download and all API calls require a proxy.
function Format-CpaConsoleText([string]$Text, [int]$Width) {
    $Text = [regex]::Replace($Text, '\x1b\[[0-9;]*m', '')
    $Text = [regex]::Replace($Text, '[\p{Cc}\p{Cf}]', ' ')
    $result = ''; $used = 0
    foreach ($character in $Text.ToCharArray()) {
        $step = if ([int]$character -ge 0x2e80) { 2 } else { 1 }
        if ($used + $step -gt $Width) { break }
        $result += $character; $used += $step
    }
    return $result + (' ' * [Math]::Max(0, $Width - $used))
}

function Write-CpaConsoleLine([string]$Text, [int]$Width, [bool]$Highlight = $false) {
    $palette = @{'31'='DarkRed';'32'='DarkGreen';'33'='DarkYellow';'34'='DarkBlue';'35'='DarkMagenta';'36'='DarkCyan';'37'='Gray';'90'='DarkGray';'91'='Red';'92'='Green';'93'='Yellow';'94'='Blue';'95'='Magenta';'96'='Cyan';'97'='White'}
    [Console]::ForegroundColor = 'White'
    [Console]::BackgroundColor = if ($Highlight) { 'DarkCyan' } else { 'Black' }
    $used = 0
    foreach ($part in [regex]::Split($Text, '(\x1b\[[0-9;]*m)')) {
        if ($part -match '^\x1b\[([0-9;]*)m$') {
            if (-not $Highlight -and $script:CpaColors) {
                foreach ($code in $Matches[1].Split(';')) {
                    if ($code -in @('', '0')) { [Console]::ForegroundColor = 'White' }
                    elseif ($palette.ContainsKey($code)) { [Console]::ForegroundColor = $palette[$code] }
                }
            }
            continue
        }
        $safe = [regex]::Replace($part, '[\p{Cc}\p{Cf}]', ' ')
        $chunk = ''
        foreach ($character in $safe.ToCharArray()) {
            $step = if ([int]$character -ge 0x2e80) { 2 } else { 1 }
            if ($used + $step -gt $Width) { break }
            $chunk += $character; $used += $step
        }
        if ($chunk) { [Console]::Write($chunk) }
        if ($used -ge $Width) { break }
    }
    [Console]::ForegroundColor = 'White'
    [Console]::Write((' ' * [Math]::Max(0, $Width - $used)))
    [Console]::BackgroundColor = 'Black'
}

function Show-CpaConsoleFrame([string]$Title, [string[]]$Lines, [string]$Footer, [int]$Selected = -1, [string]$Notice = '') {
    $width = [Math]::Max(1, [Math]::Min([Console]::WindowWidth, [Console]::BufferWidth) - 1)
    $height = [Math]::Max(1, [Math]::Min([Console]::WindowHeight, [Console]::BufferHeight))
    if ($null -eq $script:CpaColors) { $script:CpaColors = -not [bool]$env:NO_COLOR }
    if (-not $script:CpaFrameCache -or $script:CpaFrameSize -ne "$width,$height") {
        $script:CpaFrameCache = @{}; $script:CpaFrameSize = "$width,$height"
    }
    [Console]::CursorVisible = $false
    for ($row = 0; $row -lt $height; $row++) {
        $text = ''; $highlight = $false
        if ($row -eq 0) { $text = "$([char]27)[96m " + $Title }
        elseif ($row -eq 1) { $text = "$([char]27)[90m" + ('-' * $width) }
        elseif ($row -eq $height - 2) { $text = "$([char]27)[93m" + $Notice }
        elseif ($row -eq $height - 1) { $text = "$([char]27)[37m" + $Footer }
        elseif ($row - 2 -lt $Lines.Count) { $text = $Lines[$row - 2]; $highlight = $row - 2 -eq $Selected }
        $signature = "$highlight|$($script:CpaColors)|$text"
        if ($script:CpaFrameCache[$row] -eq $signature) { continue }
        if ($row -ge [Console]::BufferHeight) { return }
        try { [Console]::SetCursorPosition(0, $row) }
        catch { if ($_.Exception.InnerException -is [ArgumentOutOfRangeException]) { return }; throw }
        $liveWidth = [Math]::Max(1, [Math]::Min($width, [Math]::Min([Console]::WindowWidth, [Console]::BufferWidth) - 1))
        Write-CpaConsoleLine $text $liveWidth $highlight
        $script:CpaFrameCache[$row] = $signature
    }
}

function Read-CpaConsoleForm([string]$Title, [object[]]$Fields, [string[]]$Actions = @('Continue', 'Cancel'), [int]$Initial = 0) {
    if ($Host.Name -ne 'ConsoleHost' -or [Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        throw 'Run this tool in an interactive terminal.'
    }
    $foreground = [Console]::ForegroundColor; $background = [Console]::BackgroundColor
    $visible = [Console]::CursorVisible; $controlC = [Console]::TreatControlCAsInput
    $encoding = [Console]::OutputEncoding
    $values = @($Fields | ForEach-Object { $_.Value })
    $positions = @($values | ForEach-Object { ([string]$_).Length })
    $replace = @($Fields | ForEach-Object { $true })
    $selected = $Initial; $notice = ''
    try {
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        [Console]::TreatControlCAsInput = $true
        [Console]::Clear()
        $script:CpaFrameCache = @{}
        while ($true) {
            $lines = @()
            $available = [Math]::Max(1, [Console]::WindowWidth - 26)
            for ($i = 0; $i -lt $Fields.Count; $i++) {
                $field = $Fields[$i]
                $text = [string]$values[$i]
                if ($field.Secret) { $text = '*' * $text.Length }
                if ($field.Toggle) { $text = if ($values[$i]) { '[x]' } else { '[ ]' } }
                $start = if ($i -eq $selected -and -not $field.Toggle) { [Math]::Max(0, $positions[$i] - $available + 1) } else { 0 }
                $text = $text.Substring([Math]::Min($start, $text.Length))
                $lines += ('  ' + (Format-CpaConsoleText $field.Label 21) + ' ' + $text)
            }
            foreach ($action in $Actions) { $lines += '  ' + $action }
            Show-CpaConsoleFrame $Title $lines 'Tab / arrows: move   Enter: select   Space: toggle   Esc: exit' $selected $notice
            if ([Console]::WindowWidth -lt 52 -or [Console]::WindowHeight -lt $lines.Count + 4) {
                $notice = 'Enlarge the terminal to continue. Esc exits.'
            } else {
                if ($selected -lt $Fields.Count -and -not $Fields[$selected].Toggle) {
                    $offset = [Math]::Max(0, $positions[$selected] - $available + 1)
                    [Console]::SetCursorPosition([Math]::Min([Console]::WindowWidth - 2, 24 + $positions[$selected] - $offset), $selected + 2)
                    [Console]::CursorVisible = $true
                }
            }
            $key = [Console]::ReadKey($true)
            if ($key.Key -eq 'Escape' -or ($key.Key -eq 'C' -and ($key.Modifiers -band [ConsoleModifiers]::Control))) {
                throw [OperationCanceledException]::new('Cancelled.')
            }
            if ([Console]::WindowWidth -lt 52 -or [Console]::WindowHeight -lt $lines.Count + 4) { continue }
            $count = $Fields.Count + $Actions.Count
            if ($key.Key -in @('Tab', 'DownArrow', 'UpArrow')) {
                $step = if ($key.Key -eq 'UpArrow' -or ($key.Key -eq 'Tab' -and ($key.Modifiers -band [ConsoleModifiers]::Shift))) { -1 } else { 1 }
                $selected = ($selected + $step + $count) % $count
                continue
            }
            if ($key.Key -eq 'Enter') {
                if ($selected -ge $Fields.Count) {
                    $action = $selected - $Fields.Count
                    if ($action -ne 0) { return [pscustomobject]@{Action=$action; Values=$values} }
                    $valid = $true
                    for ($i = 0; $i -lt $Fields.Count; $i++) {
                        try { if ($Fields[$i].Validate) { $values[$i] = & $Fields[$i].Validate $values[$i] } }
                        catch { $selected = $i; $notice = $_.Exception.Message; $valid = $false; break }
                    }
                    if ($valid) { return [pscustomobject]@{Action=$action; Values=$values} }
                } else {
                    try {
                        if ($Fields[$selected].Validate) { $values[$selected] = & $Fields[$selected].Validate $values[$selected] }
                        $positions[$selected] = ([string]$values[$selected]).Length
                        $selected = ($selected + 1) % $count; $notice = ''
                    } catch { $notice = $_.Exception.Message }
                }
                continue
            }
            if ($selected -ge $Fields.Count) { continue }
            $field = $Fields[$selected]
            if ($field.Toggle) {
                if ($key.Key -in @('Spacebar', 'LeftArrow', 'RightArrow')) { $values[$selected] = -not $values[$selected] }
                continue
            }
            $value = [string]$values[$selected]; $position = $positions[$selected]
            if ($key.Modifiers -band [ConsoleModifiers]::Control) {
                if ($key.Key -eq 'U') { $values[$selected] = ''; $positions[$selected] = 0 }
                elseif ($key.Key -eq 'A') { $replace[$selected] = $true }
                continue
            }
            switch ([string]$key.Key) {
                'LeftArrow' { $positions[$selected] = [Math]::Max(0, $position - 1); $replace[$selected] = $false }
                'RightArrow' { $positions[$selected] = [Math]::Min($value.Length, $position + 1); $replace[$selected] = $false }
                'Home' { $positions[$selected] = 0; $replace[$selected] = $false }
                'End' { $positions[$selected] = $value.Length; $replace[$selected] = $false }
                'Backspace' {
                    if ($replace[$selected]) { $values[$selected] = ''; $positions[$selected] = 0 }
                    elseif ($position -gt 0) { $values[$selected] = $value.Remove($position - 1, 1); $positions[$selected]-- }
                    $replace[$selected] = $false
                }
                'Delete' {
                    if ($position -lt $value.Length) { $values[$selected] = $value.Remove($position, 1) }
                    $replace[$selected] = $false
                }
                default {
                    if (-not [char]::IsControl($key.KeyChar)) {
                        if ($replace[$selected]) { $value = ''; $position = 0 }
                        $limit = if ($field.MaxLength) { $field.MaxLength } else { 2048 }
                        if ($value.Length -lt $limit) {
                            $values[$selected] = $value.Insert($position, [string]$key.KeyChar)
                            $positions[$selected] = $position + 1
                        }
                        $replace[$selected] = $false; $notice = ''
                    }
                }
            }
        }
    } finally {
        [Console]::ForegroundColor = $foreground; [Console]::BackgroundColor = $background
        [Console]::CursorVisible = $visible; [Console]::TreatControlCAsInput = $controlC
        [Console]::OutputEncoding = $encoding
    }
}

function Read-CpaProxyPort {
    if ($Host.Name -ne 'ConsoleHost' -or [Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        throw 'Run this launcher in an interactive terminal.'
    }
    $form = Read-CpaConsoleForm 'CPA / VPN connection' @(@{Label='VPN proxy port'; Value='7890'; MaxLength=5; Validate={
        param($value)
        if ($value -cnotmatch '^[1-9][0-9]{0,4}$' -or [int]$value -gt 65535) { throw 'Enter a port from 1 to 65535.' }
        return [int]$value
    }})
    if ($form.Action -ne 0) { throw [OperationCanceledException]::new('Cancelled.') }
    return [int]$form.Values[0]
}

function Test-CpaPrivatePath([string]$Path, [bool]$Directory = $false) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.PSIsContainer -ne $Directory) {
        throw 'The configuration path must be a regular file or directory, not a link.'
    }
    if ([Environment]::OSVersion.Platform -eq 'Win32NT') {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = Get-Acl -LiteralPath $Path
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'The configuration must belong to the current user.' }
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -ne $sid.Value) {
                throw 'Other users can access the configuration.'
            }
        }
    } else {
        $isMac = [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([Runtime.InteropServices.OSPlatform]::OSX)
        $info = if ($isMac) { & stat -f '%u %Lp' $Path } else { & stat -c '%u %a' -- $Path }
        if ($LASTEXITCODE -ne 0) { throw 'Cannot verify configuration permissions.' }
        $parts = $info.Trim() -split ' '
        if ($parts[0] -ne (& id -u) -or ([Convert]::ToInt32($parts[1], 8) -band 63)) { throw 'The configuration requires owner-only permissions.' }
    }
}

function ConvertTo-CpaProxy([string]$Value) {
    $proxy = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$proxy) -or $proxy.Scheme -ne 'http' -or
        $proxy.Host -notin @('127.0.0.1', '[::1]') -or $proxy.Port -le 0 -or
        $proxy.Authority -notmatch ':\d+$' -or $proxy.AbsolutePath -ne '/' -or
        $proxy.UserInfo -or $proxy.Query -or $proxy.Fragment -or $Value -match '\s') {
        throw 'Use a loopback HTTP/mixed proxy such as http://127.0.0.1:7890. Direct access is disabled.'
    }
    return $proxy
}

function Get-CpaBootstrapConfigPath { return (Join-Path (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cpa') 'query.json') }

function Read-CpaSavedProxy {
    $path = Get-CpaBootstrapConfigPath
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    if ($Host.Name -ne 'ConsoleHost' -or [Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        throw 'Run this launcher in an interactive terminal.'
    }
    $selection = Read-CpaConsoleForm 'CPA / Saved connection' @() @('Load saved connection', 'Set up a connection', 'Exit')
    if ($selection.Action -eq 2) { throw [OperationCanceledException]::new('Cancelled.') }
    if ($selection.Action -eq 1) { return $null }
    try {
        Test-CpaPrivatePath (Split-Path $path) $true
        Test-CpaPrivatePath $path
        if ((Get-Item -LiteralPath $path -Force).Length -gt 16384) { throw 'size' }
        $data = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
        if ($data.version -ne 1) { throw 'version' }
        return ([string](ConvertTo-CpaProxy $data.proxy_url))
    } catch { return $null }
}

function Start-CpaQuota {
    $ErrorActionPreference = 'Stop'
    $script:CpaColors = -not [bool]$env:NO_COLOR
    $ProgressPreference = 'SilentlyContinue'
    try {
    $proxy = Read-CpaSavedProxy
    $choice = '1'
    if (-not $proxy) {
        $choice = '2'
        $port = Read-CpaProxyPort
        $proxy = "http://127.0.0.1:$port"
    }
    $url = 'https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/829cb0dfd634f177a7a553e9eaf7a6b477c4a9bc/scripts/cpa/quota.ps1'
    $response = Invoke-WebRequest -UseBasicParsing -Uri $url -Proxy $proxy -MaximumRedirection 0 -TimeoutSec 60
    if ($response.StatusCode -ne 200) { throw 'Client download failed; no direct fallback.' }
    $bytes = $response.RawContentStream.ToArray()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    if ($digest -cne '35944259dfa571c44d486dff611dc532ed0d477b2a229b81ba7f61d9aad3f87d') {
        throw 'Client SHA256 mismatch; nothing was executed.'
    }
    $source = [Text.Encoding]::UTF8.GetString($bytes).TrimStart([char]0xFEFF)
    $previousChoice = $env:CPA_QUERY_CHOICE
    try {
        $env:CPA_QUERY_CHOICE = $choice
        & ([scriptblock]::Create($source)) -ProxyUrl $proxy -BaseUrl ''
    } finally { $env:CPA_QUERY_CHOICE = $previousChoice }
    } catch [OperationCanceledException] { return }
}

Start-CpaQuota
