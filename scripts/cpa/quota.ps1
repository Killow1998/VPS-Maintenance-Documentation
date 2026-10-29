# Standalone client for Windows PowerShell 5.1+ and PowerShell 7+.
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$BaseUrl = $env:CPA_BASEURL,
    [string]$ProxyUrl = $env:CPA_PROXY,
    [string]$KeyEnv,
    [ValidateSet('all', 'usage', 'quota')][string]$View = 'all',
    [ValidateSet('overview', '1h', '7d', '30d', 'day', 'month', 'all')][string]$Period = 'overview',
    [string]$Date,
    [ValidateRange(0, 50)][int]$Recent = 10,
    [switch]$Tui,
    [switch]$Once
)

$explicitTui = $PSBoundParameters.ContainsKey('Tui')
$noArguments = $PSBoundParameters.Count -eq 0

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

function Read-CpaConsoleKey {
    # ReadKey blocks on input; observe window changes before waiting for a key.
    while ($true) {
        $width = [Math]::Max(1, [Math]::Min([Console]::WindowWidth, [Console]::BufferWidth) - 1)
        $height = [Math]::Max(1, [Math]::Min([Console]::WindowHeight, [Console]::BufferHeight))
        if ($script:CpaFrameSize -ne "$width,$height") { return $null }
        if ([Console]::KeyAvailable) { return [Console]::ReadKey($true) }
        Start-Sleep -Milliseconds 50
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
            $key = Read-CpaConsoleKey
            if ($null -eq $key) { continue }
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

function Test-CpaTerminal {
    return ($Host.Name -eq 'ConsoleHost' -and -not [Console]::IsInputRedirected -and
        -not [Console]::IsOutputRedirected)
}

function Read-CpaSetting([string]$Prompt, [scriptblock]$Validate) {
    if (-not (Test-CpaTerminal)) {
        throw '交互设置需要终端；自动化请指定 BaseUrl、ProxyUrl 和 KeyEnv。'
    }
    while ($true) {
        $value = Read-Host $Prompt
        if ($null -eq $value) { throw '已取消输入。' }
        try { return (& $Validate $value.Trim()) }
        catch { Write-Host $_.Exception.Message }
    }
}

function ConvertTo-CpaProxy([string]$Value) {
    $proxy = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$proxy) -or $proxy.Scheme -ne 'http' -or
        $proxy.Host -notin @('127.0.0.1', '[::1]') -or $proxy.Port -le 0 -or
        $proxy.Authority -notmatch ':\d+$' -or $proxy.AbsolutePath -ne '/' -or
        $proxy.UserInfo -or $proxy.Query -or $proxy.Fragment -or $Value -match '\s') {
        throw '必须用 -ProxyUrl 或 CPA_PROXY 指定本机 HTTP/mixed 代理，例如 http://127.0.0.1:7890；禁止直连。'
    }
    return $proxy
}

function Get-CpaConfigPath { return (Join-Path (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cpa') 'query.json') }

function Test-CpaPrivatePath([string]$Path, [bool]$Directory = $false) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.PSIsContainer -ne $Directory) {
        throw '配置路径必须是普通目录或文件，不能是链接。'
    }
    if ([Environment]::OSVersion.Platform -eq 'Win32NT') {
        $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl = Get-Acl -LiteralPath $Path
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw '配置不归当前用户所有。' }
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -ne $sid.Value) {
                throw '配置允许其他用户访问。'
            }
        }
    } else {
        $isMac = [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([Runtime.InteropServices.OSPlatform]::OSX)
        $info = if ($isMac) { & stat -f '%u %Lp' $Path } else { & stat -c '%u %a' -- $Path }
        if ($LASTEXITCODE -ne 0) { throw '无法核对配置权限。' }
        $parts = $info.Trim() -split ' '
        if ($parts[0] -ne (& id -u) -or ([Convert]::ToInt32($parts[1], 8) -band 63)) { throw '配置需要当前用户独占权限。' }
    }
}

function Read-CpaConfig {
    $path = Get-CpaConfigPath
    try {
        Test-CpaPrivatePath (Split-Path $path) $true
        Test-CpaPrivatePath $path
        if ((Get-Item -LiteralPath $path -Force).Length -gt 16384) { throw 'size' }
        $saved = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
        if ($saved.version -ne 1 -or $saved.api_key -isnot [string] -or $saved.api_key -cnotmatch '^[\x21-\x7e]{1,1024}$') { throw 'format' }
        $saved.base_url = ConvertTo-CpaBase $saved.base_url
        $null = ConvertTo-CpaProxy $saved.proxy_url
        return $saved
    } catch { throw '已存配置无法读取、权限不安全或内容无效；请重新输入。' }
}

function Save-CpaConfig([string]$Base, [string]$Proxy, [string]$Key) {
    $path = Get-CpaConfigPath; $directory = Split-Path $path; $temporary = $null
    try {
        if (-not (Test-Path -LiteralPath $directory)) {
            $null = New-Item -ItemType Directory -Path $directory -ErrorAction Stop
            if ([Environment]::OSVersion.Platform -eq 'Win32NT') {
                $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
                $acl = New-Object System.Security.AccessControl.DirectorySecurity
                $acl.SetAccessRuleProtection($true, $false); $acl.SetOwner($sid)
                $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
                Set-Acl -LiteralPath $directory -AclObject $acl -ErrorAction Stop
            } else { & chmod 700 $directory; if ($LASTEXITCODE -ne 0) { throw 'permissions' } }
        }
        Test-CpaPrivatePath $directory $true
        if (Test-Path -LiteralPath $path) { Test-CpaPrivatePath $path }
        $temporary = Join-Path $directory ('.query-' + [Guid]::NewGuid().ToString('N'))
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Dispose()
        if ([Environment]::OSVersion.Platform -eq 'Win32NT') {
            $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
            $acl = New-Object System.Security.AccessControl.FileSecurity
            $acl.SetAccessRuleProtection($true, $false); $acl.SetOwner($sid)
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow'))
            Set-Acl -LiteralPath $temporary -AclObject $acl -ErrorAction Stop
        } else { & chmod 600 $temporary; if ($LASTEXITCODE -ne 0) { throw 'permissions' } }
        $data = @{version=1; base_url=$Base; proxy_url=$Proxy; api_key=$Key} | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($temporary, $data + "`n", [Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $path) { [IO.File]::Replace($temporary, $path, [System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temporary, $path) }
        $temporary = $null
    } catch { throw '配置未保存；请检查 ~/.cpa 的权限和可用空间。' }
    finally { if ($temporary -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary -Force } }
}

function ConvertTo-CpaBase([string]$Value) {
    $base = $Value.TrimEnd('/')
    if ($base.EndsWith('/v1')) { $base = $base.Substring(0, $base.Length - 3) }
    $uri = $null
    if (-not [Uri]::TryCreate($base, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne 'https' -or
        -not $uri.Host -or $uri.Port -eq 0 -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $base -match '[\s\p{Cc}]') {
        throw '请提供 HTTPS Base URL，不包含用户名、密码、查询参数或片段。'
    }
    return $base
}

function Get-QuotaTime($Value) {
    if ($Value -is [DateTimeOffset]) { return $Value }
    if ($Value -is [DateTime] -and $Value.Kind -ne [DateTimeKind]::Unspecified) {
        return [DateTimeOffset]$Value
    }
    $target = [DateTimeOffset]::MinValue
    if ($Value -is [string] -and $Value -match '(Z|[+-]\d\d:\d\d)$' -and
        [DateTimeOffset]::TryParse($Value, [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None, [ref]$target)) { return $target }
    return $null
}

function Get-ResetLabel($Value) {
    $target = Get-QuotaTime $Value
    if ($null -eq $target) { return '未知' }
    $seconds = [long][Math]::Max(0, [Math]::Floor(($target - [DateTimeOffset]::UtcNow).TotalSeconds))
    if ($seconds -ge 86400) {
        $left = '{0}d{1}h' -f [Math]::Floor($seconds / 86400), [Math]::Floor(($seconds % 86400) / 3600)
    } elseif ($seconds -ge 3600) {
        $left = '{0}h{1}m' -f [Math]::Floor($seconds / 3600), [Math]::Floor(($seconds % 3600) / 60)
    } elseif ($seconds -ge 60) {
        $left = '{0}m{1}s' -f [Math]::Floor($seconds / 60), ($seconds % 60)
    } else { $left = '{0}s' -f $seconds }
    return $target.ToOffset([TimeSpan]::FromHours(8)).ToString('MM-dd HH:mm') + " ($left left)"
}

function Get-WindowLabel([string]$Name) {
    $label = $Name -replace '^five_hour', '5 小时' -replace '^seven_day', '7 天' -replace '_', ' '
    return Format-Cell ([Globalization.CultureInfo]::InvariantCulture.TextInfo.ToTitleCase($label)) 16
}

function Get-Remaining($Value) {
    if ($null -eq $Value -or $Value -is [bool] -or $Value -is [string]) { return $null }
    try { $number = [double]$Value } catch { return $null }
    if ([double]::IsNaN($number) -or [double]::IsInfinity($number) -or $number -lt 0 -or $number -gt 100) {
        return $null
    }
    return $number
}


function Format-Cell($Value, [int]$Size) {
    $text = [regex]::Replace([string]$Value, '[\p{Cc}\p{Cf}]', ' ')
    $result = ''; $width = 0
    foreach ($char in $text.ToCharArray()) {
        $step = if ([int]$char -ge 0x2e80) { 2 } else { 1 }
        if ($width + $step -gt $Size) { break }
        $result += $char; $width += $step
    }
    return $result + (' ' * ($Size - $width))
}

function Format-Number($Value) {
    if ($null -eq $Value -or $Value -is [bool] -or $Value -is [string]) { return '未知' }
    return $Value.ToString('N0', [Globalization.CultureInfo]::InvariantCulture)
}

function Get-PublicAccount($Value) {
    if ($Value -is [string] -and $Value -cmatch '^account-[0-9a-f]{12}$') { return $Value }
    return '未归属'
}

function Get-Beijing($Value, [switch]$Seconds) {
    $target = Get-QuotaTime $Value
    if ($null -eq $target) { return '未知' }
    $format = if ($Seconds) { 'MM-dd HH:mm:ss' } else { 'MM-dd HH:mm' }
    return $target.ToOffset([TimeSpan]::FromHours(8)).ToString($format)
}

function Get-Cost($Row) {
    $low = $Row.estimated_cost_usd_low; $high = $Row.estimated_cost_usd_high
    foreach ($value in @($low, $high)) {
        if ($null -eq $value -or $value -is [bool] -or $value -is [string]) { return '未知' }
        if ([double]::IsNaN([double]$value) -or [double]::IsInfinity([double]$value)) { return '未知' }
    }
    if ($low -lt 0 -or $high -lt $low) { return '未知' }
    $format = if (($low -gt 0 -and $low -lt .0001) -or ($high -gt 0 -and $high -lt .0001)) { 'F8' } else { 'F4' }
    $left = '$' + $low.ToString($format, [Globalization.CultureInfo]::InvariantCulture)
    $right = '$' + $high.ToString($format, [Globalization.CultureInfo]::InvariantCulture)
    if ($left -ne $right) { $left += '–' + $right }
    if ($null -ne $Row.priced_requests -and $null -ne $Row.requests -and
        $Row.priced_requests -ge 0 -and $Row.priced_requests -lt $Row.requests) { $left = '*' + $left }
    return $left
}

function Get-CachePercent($Row) {
    $read = $Row.cache_read_tokens; $total = $Row.input_tokens
    foreach ($value in @($read, $total)) {
        if ($null -eq $value -or $value -is [bool] -or $value -is [string]) { return $null }
        if ([double]::IsNaN([double]$value) -or [double]::IsInfinity([double]$value)) { return $null }
    }
    if ($total -le 0 -or $read -lt 0 -or $read -gt $total) { return $null }
    return ([double]$read / $total * 100)
}

function Get-CacheLabel($Row) {
    $value = Get-CachePercent $Row
    if ($null -eq $value) { return '未知' }
    return $value.ToString('F1', [Globalization.CultureInfo]::InvariantCulture) + '%'
}

function Format-Compact($Value) {
    if ($null -eq $Value -or $Value -is [bool] -or $Value -is [string] -or $Value -lt 0) { return '未知' }
    foreach ($scale in @(@(1e12, 'T'), @(1e9, 'B'), @(1e6, 'M'), @(1e3, 'K'))) {
        if ($Value -ge $scale[0] * 10) {
            return ($Value / $scale[0]).ToString('F2', [Globalization.CultureInfo]::InvariantCulture) + $scale[1]
        }
    }
    return Format-Number $Value
}

function Get-Coverage($Row) {
    return (Format-Compact $Row.priced_requests) + '/' + (Format-Compact $Row.requests)
}

function Show-Models($Models, [int]$Page = 0, [bool]$Paged = $false) {
    if ($null -eq $Models) {
        Write-Host '服务尚未提供模型分项，请更新 VPS observer；不能据此判断 Key 没有调用。'
        return
    }
    $rows = @($Models | Sort-Object @{Expression='estimated_cost_usd_high';Descending=$true},
        @{Expression='total_tokens';Descending=$true}, model)
    if (-not $rows.Count) { Write-Host '选定期间暂无已采集调用。'; return }
    $pages = [int][Math]::Ceiling($rows.Count / 5.0)
    if ($Paged) {
        $Page = (($Page % $pages) + $pages) % $pages
        Write-Host ('模型 {0} 个 · 第 {1}/{2} 页' -f $rows.Count, ($Page + 1), $pages)
        $rows = @($rows | Select-Object -Skip ($Page * 5) -First 5)
    }
    Write-Host ('{0} {1} {2} {3} {4} 计价覆盖' -f (Format-Cell '模型' 18), (Format-Cell '请求' 7),
        (Format-Cell 'Tokens' 9), (Format-Cell '缓存' 6), (Format-Cell '估算 USD' 23))
    foreach ($row in $rows) {
        $name = [string]$row.model
        if ($name.Length -gt 18) { Write-Host (Format-Cell $name 160).Trim(); $name = '  >' }
        Write-Host ('{0} {1} {2} {3} {4} {5}' -f (Format-Cell $name 18), (Format-Cell (Format-Compact $row.requests) 7),
            (Format-Cell (Format-Compact $row.total_tokens) 9), (Format-Cell (Get-CacheLabel $row) 6),
            (Format-Cell (Get-Cost $row) 23), (Get-Coverage $row))
    }
}

function Show-Usage($Data, [string]$Selected = '1h', [int]$Page = 0, [bool]$Paged = $false) {
    if ($Period -eq 'overview') {
        if ($Data.period -cne 'overview' -or @($Data.windows).Count -ne 4 -or
            (($Data.windows | ForEach-Object { $_.period }) -join ',') -cne '1h,7d,30d,all') {
            throw '服务未提供多时间段总览，请更新 VPS observer。'
        }
        Write-Host ('本 Key · 用量总览 · 截止 {0}（北京）' -f (Get-Beijing $Data.as_of -Seconds))
        Write-Host ('{0} {1} {2} {3} {4} 计价覆盖' -f (Format-Cell '时间段' 10), (Format-Cell '请求/失败' 11),
            (Format-Cell 'Tokens' 9), (Format-Cell '缓存' 7), (Format-Cell '估算 USD' 23))
        foreach ($row in $Data.windows) {
            $label = switch ($row.period) { '1h' { '近 1 小时' } '7d' { '近 7 天' } '30d' { '近 30 天' } 'all' { '总历史' } }
            $total = $row.totals
            Write-Host ('{0} {1} {2} {3} {4} {5}' -f (Format-Cell $label 10),
                (Format-Cell ((Format-Compact $total.requests) + '/' + (Format-Compact $total.failed_requests)) 11),
                (Format-Cell (Format-Compact $total.total_tokens) 9), (Format-Cell (Get-CacheLabel $total) 7),
                (Format-Cell (Get-Cost $total) 23), (Get-Coverage $total))
        }
        $window = $Data.windows | Where-Object { $_.period -eq $Selected } | Select-Object -First 1
        $label = switch ($Selected) { '1h' { '近 1 小时' } '7d' { '近 7 天' } '30d' { '近 30 天' } 'all' { '总历史' } }
        Write-Host "`n模型明细 · $label · 按估算 USD 降序"
        Write-Host ('输入 {0} · 输出 {1}' -f (Format-Number $window.totals.input_tokens), (Format-Number $window.totals.output_tokens))
        Write-Host ('缓存读 {0} · 缓存写 {1}' -f (Format-Number $window.totals.cache_read_tokens), (Format-Number $window.totals.cache_write_tokens))
        Show-Models $window.models $Page $Paged
        Write-Host '缓存 = 缓存读 / 全部输入 token；* 部分计价，缺失不计为零；USD 不是订阅扣款。'
        Write-Host ('采集开始 {0} · 采集至 {1} · 状态 {2} · 拒收 {3}' -f
            (Get-Beijing $Data.collection_started_at), (Get-Beijing $Data.last_collected_at -Seconds),
            (Format-Cell $Data.collector_status 24).Trim(), (Format-Number $Data.rejected_events))
        Write-Host '滚动窗口相互重叠，不可相加；总历史仅含已采集并保留的数据，可能存在采集缺口。'
        return
    }
    $periodLabel = switch ($Data.period) {
        '1h' { '近 1 小时' } '7d' { '近 7 天' } '30d' { '近 30 天' }
        'day' { '今日' } 'month' { '本月' } default { '全部' }
    }
    if ($Data.date) { $periodLabel = (Format-Cell $Data.date 10).Trim() }
    $total = $Data.totals
    Write-Host "本 Key · ${periodLabel}用量（北京）"
    Write-Host ('请求 {0} · 失败 {1} · Tokens {2} · API 标价估算 {3}' -f `
        (Format-Number $total.requests), (Format-Number $total.failed_requests), (Format-Number $total.total_tokens), (Get-Cost $total))
    Write-Host ('输入 {0} · 输出 {1} · 缓存读 {2} · 缓存写 {3}' -f `
        (Format-Number $total.input_tokens), (Format-Number $total.output_tokens), (Format-Number $total.cache_read_tokens), (Format-Number $total.cache_write_tokens))
    Write-Host ('计价覆盖 {0}/{1} · 采集至 {2} · 开始 {3}' -f (Format-Number $total.priced_requests), `
        (Format-Number $total.requests), (Get-Beijing $Data.last_collected_at -Seconds), (Get-Beijing $Data.collection_started_at))
    Write-Host ('Token 缓存命中率 {0} · * 部分计价，不是全部费用' -f (Get-CacheLabel $total))
    Show-Models $Data.models
    if (-not $Data.PSObject.Properties['accounts'] -or -not $Data.PSObject.Properties['recent']) {
        Write-Host '服务尚未提供调用账号与明细，请更新 VPS observer。'
        return
    }
    Write-Host "`n按调用账号汇总 · 仅本 Key"
    Write-Host ('{0} {1} {2} {3} API 标价估算' -f (Format-Cell '匿名账号' 21), (Format-Cell '请求' 7), (Format-Cell '失败' 5), (Format-Cell 'Tokens' 13))
    foreach ($row in $Data.accounts) {
        Write-Host ('{0} {1} {2} {3} {4}' -f (Format-Cell (Get-PublicAccount $row.id) 21), `
            (Format-Cell (Format-Number $row.requests) 7), (Format-Cell (Format-Number $row.failed_requests) 5), `
            (Format-Cell (Format-Number $row.total_tokens) 13), (Get-Cost $row))
    }
    if (@($Data.accounts).Count -eq 0) { Write-Host '选定期间暂无已采集调用。' }
    Write-Host "`n近期调用 · 选定期间最近 $($Data.recent_limit) 条 · 北京时间"
    Write-Host ('{0} {1} {2} {3} 状态' -f (Format-Cell '时间' 14), (Format-Cell '匿名账号' 21), (Format-Cell '模型' 24), (Format-Cell 'Tokens' 13))
    foreach ($row in $Data.recent) {
        $state = if ($row.failed) { '失败' } else { '成功' }
        Write-Host ('{0} {1} {2} {3} {4}' -f (Format-Cell (Get-Beijing $row.timestamp -Seconds) 14), `
            (Format-Cell (Get-PublicAccount $row.account_id) 21), (Format-Cell $row.model 24), `
            (Format-Cell (Format-Number $row.total_tokens) 13), $state)
    }
    if (@($Data.recent).Count -eq 0) { Write-Host '暂无记录。' }
    Write-Host '输入已含缓存，输出已含 reasoning；API 标价估算不代表订阅扣款；重试按多次上游执行记录。'
}

function Show-Quota($Data) {
    $accounts = @($data.accounts)
    $enabled = @($accounts | Where-Object { $_.enabled -eq $true }).Count
    $fresh = @($accounts | Where-Object { $_.enabled -eq $true -and $_.stale -eq $false }).Count
    $checked = Get-QuotaTime $data.checked_at
    $checkedText = if ($null -ne $checked) { $checked.ToOffset([TimeSpan]::FromHours(8)).ToString('MM-dd HH:mm') } else { '未知' }
    Write-Host "匿名号池 · 剩余额度`n$($accounts.Count) 个账号 · $enabled 启用 · $fresh 新鲜 · 更新 ${checkedText}（北京）"
    if ($accounts.Count -eq 0) { Write-Host '暂无账号额度，请检查 observer 和上游授权。' }
    $barSize = 20; $index = 0
    foreach ($account in $accounts) {
        $index++
        $identity = if ($account.id -cmatch '^account-[0-9a-f]{12}$') { $account.id } else { 'account-{0:00}' -f $index }
        $stale = $account.enabled -ne $true -or $account.stale -ne $false
        $state = if ($account.enabled -ne $true) { '已禁用' } elseif ($stale) { '旧值 / 未知' } else { '正常' }
        Write-Host "`n$identity · $state"
        $rows = @($account.windows.PSObject.Properties | Where-Object { $_.Name -cmatch '^(five_hour|seven_day)(_[a-z0-9_]+)?$' })
        if ($rows.Count -eq 0) { Write-Host '  暂无额度窗口' }
        foreach ($row in $rows) {
            $value = Get-Remaining $row.Value.remaining_percent
            $filled = if ($null -ne $value) { [int][Math]::Floor($barSize * $value / 100 + 0.5) } else { 0 }
            $bar = '[' + ('█' * $filled) + ('░' * ($barSize - $filled)) + ']'
            $percent = if ($null -ne $value) { $value.ToString('0.0', [Globalization.CultureInfo]::InvariantCulture).PadLeft(5) + '%' } else { '  未知' }
            $color = if ($stale -or $null -eq $value) { 'DarkGray' } elseif ($value -gt 50) { 'Green' } elseif ($value -gt 20) { 'Yellow' } else { 'Red' }
            Write-Host ('  ' + (Get-WindowLabel $row.Name) + ' ') -NoNewline
            if (-not [bool]$env:NO_COLOR) {
                Write-Host "$bar $percent" -ForegroundColor $color -NoNewline
            } else { Write-Host "$bar $percent" -NoNewline }
            Write-Host ('  重置 ' + (Get-ResetLabel $row.Value.resets_at))
        }
    }
    Write-Host "`n█ 剩余  ░ 已用 · 时间为北京时间 · 灰色=旧值/未知 · 各账号百分比不可相加"
}

function Get-CpaData($Base, $Headers, $Path, $Scope, $Proxy) {
    try {
        $data = Invoke-RestMethod -Uri ($Base + $Path) -Headers $Headers -Method Get `
            -Proxy $Proxy -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 30 -ErrorAction Stop
    } catch {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        $message = switch ($status) {
            400 { '查询参数不受支持；总览/滚动时间段需要新版 observer' }
            401 { 'Key 无效或已撤销' }
            404 { '入口未配置用量/额度接口' }
            429 { '查询过于频繁，请稍后再试' }
            default { '请检查指定代理、TLS 和入口服务；不会回退直连或跟随重定向' }
        }
        throw "查询失败（HTTP $status）：$message。"
    }
    if ($data.scope -cne $Scope) { throw '接口返回的数据范围错误，请检查 observer 路由。' }
    if ($Scope -eq 'shared_pool' -and $data.accounts -isnot [Array]) { throw '此入口未返回匿名号池。' }
    if ($Scope -eq 'own_key' -and -not $data.totals -and -not $data.windows) { throw '此入口未返回本 Key 用量。' }
    return $data
}

function Read-CpaConnection([string]$Proxy, [string]$Base, [string]$Key = '') {
    $port = '7890'; $proxyHost = '127.0.0.1'
    if ($Proxy) { $uri = ConvertTo-CpaProxy $Proxy; $port = [string]$uri.Port; $proxyHost = $uri.Host }
    $fields = @(
        @{Label='VPN 代理端口'; Value=$port; MaxLength=5; Validate={
            param($value)
            if ($value -cnotmatch '^[1-9][0-9]{0,4}$' -or [int]$value -gt 65535) { throw '代理端口必须为 1–65535。' }
            return [string][int]$value
        }},
        @{Label='HTTPS Base URL'; Value=$Base; Validate={param($value) ConvertTo-CpaBase $value}},
        @{Label='API Key'; Value=$Key; Secret=$true; MaxLength=1024; Validate={
            param($value)
            if ($value -cnotmatch '^[\x21-\x7e]{1,1024}$') { throw '请输入有效的 API Key。' }
            return $value
        }},
        @{Label='记住连接（含 Key）'; Value=$false; Toggle=$true}
    )
    $initial = if ($Proxy) { 1 } else { 0 }
    $form = Read-CpaConsoleForm 'CPA / 连接设置' $fields @('连接', '退出') -Initial $initial
    if ($form.Action -ne 0) { throw [OperationCanceledException]::new('Cancelled.') }
    return [pscustomobject]@{Proxy=('http://' + $proxyHost + ':' + $form.Values[0]); Base=$form.Values[1]; Key=$form.Values[2]; Remember=[bool]$form.Values[3]}
}

function Get-CpaTint([string]$Text, [int]$Color = 97) {
    return "$([char]27)[$($Color)m$Text$([char]27)[0m"
}

function Get-CpaDashboardLines($Results, $Failures, $Views, [string]$Selected, [int]$Page, [string]$Panel = 'overview', [int]$Width = 100) {
    $lines = [Collections.Generic.List[string]]::new()
    $tabs = @()
    foreach ($tab in @(@('overview','总览'), @('models','模型明细'), @('quota','账号额度'))) {
        $tabs += if ($tab[0] -eq $Panel) { Get-CpaTint ("[" + $tab[1] + "]") 96 } else { Get-CpaTint (" " + $tab[1] + " ") 90 }
    }
    $lines.Add(($tabs -join '   ')); $lines.Add('')
    $usage = $Results['usage']; $quota = $Results['quota']
    $window = $null
    if ($usage -and -not $Failures.ContainsKey('usage')) {
        if ($usage.period -cne 'overview' -or (($usage.windows | ForEach-Object { $_.period }) -join ',') -cne '1h,7d,30d,all') {
            $Failures['usage'] = '服务未提供多时间段总览，请更新 observer。'
        } else { $window = $usage.windows | Where-Object { $_.period -eq $Selected } | Select-Object -First 1 }
    }
    if ($Panel -ne 'quota') {
        $lines.Add((Get-CpaTint '── 本 Key 用量' 96) + (Get-CpaTint ('  截止 ' + (Get-Beijing $usage.as_of -Seconds) + ' 北京') 90))
        if ($Failures.ContainsKey('usage')) { $lines.Add((Get-CpaTint $Failures['usage'] 91)) }
        elseif ($window) {
            $lines.Add((Get-CpaTint ('{0} {1} {2} {3} {4}' -f (Format-Cell '期间' 10), (Format-Cell '请求/失败' 12), (Format-Cell 'Tokens' 12), (Format-Cell '缓存' 8), '估算 USD') 90))
            foreach ($row in $usage.windows) {
                $label = @{'1h'='1 近1小时';'7d'='2 近7天';'30d'='3 近30天';'all'='4 总历史'}[$row.period]
                $total = $row.totals
                $color = if ($row.period -eq $Selected) { 96 } else { 37 }
                $lines.Add((Get-CpaTint (Format-Cell $label 10) $color) + ' ' +
                    (Format-Cell ((Format-Compact $total.requests) + '/' + (Format-Compact $total.failed_requests)) 12) + ' ' +
                    (Get-CpaTint (Format-Cell (Format-Compact $total.total_tokens) 12) 96) + ' ' +
                    (Get-CpaTint (Format-Cell (Get-CacheLabel $total) 8) 92) + ' ' + (Get-CpaTint (Get-Cost $total) 93))
            }
        }
        $lines.Add('')
    }
    if ($Panel -ne 'models') {
        $accounts = @($quota.accounts)
        $active = @($accounts | Where-Object { $_.enabled -eq $true }).Count
        $checked = Get-Beijing $quota.checked_at
        $lines.Add((Get-CpaTint '── 账号额度' 96) + (Get-CpaTint ("  $active/$($accounts.Count) 启用 · 剩余 · 更新 $checked 北京") 90))
        if ($Failures.ContainsKey('quota')) { $lines.Add((Get-CpaTint $Failures['quota'] 91)) }
        elseif (-not $accounts.Count) { $lines.Add((Get-CpaTint '暂无账号额度' 90)) }
        else {
            $quotaLines = [Collections.Generic.List[string]]::new(); $index = 0
            foreach ($account in $accounts) {
                $index++
                $stale = $account.enabled -ne $true -or $account.stale -ne $false
                $state = if ($account.enabled -ne $true) { '禁用' } elseif ($stale) { '旧值' } else { '' }
                if ($Panel -eq 'quota') {
                    $identity = if ($account.id -cmatch '^account-[0-9a-f]{12}$') { $account.id } else { 'account-{0:00}' -f $index }
                    $quotaLines.Add((Get-CpaTint ("A{0:00}  {1}  {2}" -f $index, $identity, $state) 37))
                }
                $rows = @($account.windows.PSObject.Properties | Where-Object { $_.Name -cmatch '^(five_hour|seven_day)(_[a-z0-9_]+)?$' })
                if (-not $rows.Count) { $quotaLines.Add((Get-CpaTint ("A{0:00}  暂无额度窗口" -f $index) 90)) }
                foreach ($row in $rows) {
                    $value = Get-Remaining $row.Value.remaining_percent
                    $color = if ($stale -or $null -eq $value) { 90 } elseif ($value -gt 50) { 92 } elseif ($value -gt 20) { 93 } else { 91 }
                    $size = if ($Width -ge 100) { 24 } else { 16 }
                    $filled = if ($null -eq $value) { 0 } else { [int][Math]::Floor($size * $value / 100 + .5) }
                    $bar = (Get-CpaTint ('█' * $filled) $color) + (Get-CpaTint ('░' * ($size - $filled)) 90)
                    $percent = if ($null -eq $value) { '  未知' } else { $value.ToString('0.0', [Globalization.CultureInfo]::InvariantCulture).PadLeft(5) + '%' }
                    $label = "A{0:00} {1}" -f $index, (Get-WindowLabel $row.Name)
                    $quotaLines.Add((Format-Cell $label 15) + ' ' + $bar + ' ' + (Get-CpaTint $percent $color) + ' ' +
                        (Get-CpaTint ($state + ' ' + (Get-ResetLabel $row.Value.resets_at)) 37))
                }
                if ($Panel -eq 'quota') { $quotaLines.Add('') }
            }
            $limit = if ($Panel -eq 'overview') { 4 } else { $quotaLines.Count }
            foreach ($line in ($quotaLines | Select-Object -First $limit)) { $lines.Add($line) }
            if ($quotaLines.Count -gt $limit) { $lines.Add((Get-CpaTint ("另 {0} 条，Tab 查看账号额度" -f ($quotaLines.Count - $limit)) 90)) }
        }
        $lines.Add('')
    }
    if ($Panel -ne 'quota' -and $window) {
        $rows = @($window.models | Sort-Object @{Expression='estimated_cost_usd_high';Descending=$true}, @{Expression='total_tokens';Descending=$true}, model)
        $size = if ($Panel -eq 'overview') { 3 } else { 5 }
        $pages = [Math]::Max(1, [int][Math]::Ceiling($rows.Count / [double]$size))
        $pageIndex = (($Page % $pages) + $pages) % $pages
        $lines.Add((Get-CpaTint "── 模型 / $Selected" 96) + (Get-CpaTint ("  $($rows.Count) 个 · 第 $($pageIndex + 1)/$pages 页 · USD 降序") 90))
        if ($null -eq $window.models) { $lines.Add((Get-CpaTint '接口缺少模型明细，请更新 observer' 93)) }
        elseif (-not $rows.Count) { $lines.Add((Get-CpaTint '选定期间暂无已采集调用' 90)) }
        else {
            $nameWidth = if ($Width -ge 100) { 30 } else { 24 }
            $lines.Add((Get-CpaTint ('{0} {1} {2} {3} 估算 USD' -f (Format-Cell '模型' $nameWidth), (Format-Cell '请求' 7).TrimEnd().PadLeft(5), (Format-Cell 'Tokens' 11).TrimEnd().PadLeft(11), (Format-Cell '缓存' 7).TrimEnd().PadLeft(5)) 90))
            $index = $pageIndex * $size
            foreach ($row in ($rows | Select-Object -Skip $index -First $size)) {
                $color = @(96,92,95,93)[$index % 4]; $index++
                $lines.Add((Get-CpaTint (Format-Cell ([string]$row.model) $nameWidth) $color) + ' ' +
                    (Format-Cell (Format-Compact $row.requests) 7).TrimEnd().PadLeft(7) + ' ' + (Format-Cell (Format-Compact $row.total_tokens) 11).TrimEnd().PadLeft(11) + ' ' +
                    (Get-CpaTint (Format-Cell (Get-CacheLabel $row) 7).TrimEnd().PadLeft(7) 92) + ' ' + (Get-CpaTint (Get-Cost $row) 93))
            }
        }
        if ($Panel -eq 'models') {
            $lines.Add('')
            $lines.Add((Get-CpaTint ('输入 {0}  输出 {1}  缓存读 {2}  写 {3}' -f (Format-Compact $window.totals.input_tokens),
                (Format-Compact $window.totals.output_tokens), (Format-Compact $window.totals.cache_read_tokens), (Format-Compact $window.totals.cache_write_tokens)) 37))
            $lines.Add((Get-CpaTint ('计价覆盖 ' + (Get-Coverage $window.totals) + ' · * 仅含已计价费用') 90))
            $lines.Add((Get-CpaTint ('采集至 ' + (Get-Beijing $usage.last_collected_at -Seconds) + ' 北京 · ' +
                (Format-Cell $usage.collector_status 20).Trim() + ' · 拒收 ' + (Format-Number $usage.rejected_events)) 90))
        }
    }
    return $lines.ToArray()
}

function Read-CpaDashboardAction {
    while ($true) {
        $key = Read-CpaConsoleKey
        if ($null -eq $key) { return 'resize' }
        if ($key.Key -eq 'Escape' -or ($key.Key -eq 'C' -and ($key.Modifiers -band [ConsoleModifiers]::Control))) { return 'q' }
        switch ([string]$key.Key) {
            'Enter' { return 'r' }
            'Tab' { if ($key.Modifiers -band [ConsoleModifiers]::Shift) { return 'backtab' }; return 'tab' }
            'UpArrow' { return 'up' }
            'DownArrow' { return 'down' }
            'PageUp' { return 'pageup' }
            'PageDown' { return 'pagedown' }
            'LeftArrow' { return 'left' }
            'RightArrow' { return 'right' }
            'Home' { return 'home' }
            'End' { return 'end' }
        }
        $action = ([string]$key.KeyChar).ToLowerInvariant()
        if ($action -in @('q','r','1','2','3','4','n','p','c')) { return $action }
    }
}

function Invoke-CpaReport {
    $ErrorActionPreference = 'Stop'
    $script:CpaColors = -not [bool]$env:NO_COLOR
    if ($Once -and $Tui) { throw 'Once 和 Tui 不能同时使用。' }
    if ($Date -and $Period -notin @('day', 'month')) { throw 'Date 仅支持 day/month。' }
    $interactive = Test-CpaTerminal
    if ($Tui -and ($View -ne 'all' -or $Period -ne 'overview' -or -not $interactive)) {
        throw 'TUI 需要交互终端且使用默认 all/overview；单次查询请用 -Once。'
    }
    $wantTui = $interactive -and -not $Once -and (-not $explicitTui -or $Tui) -and $View -eq 'all' -and $Period -eq 'overview'
    $saved = $null
    if ($interactive) {
        $choice = $env:CPA_QUERY_CHOICE
        if (-not $choice -and -not ($BaseUrl -or $ProxyUrl -or $KeyEnv) -and (Test-Path -LiteralPath (Get-CpaConfigPath))) {
            if ($wantTui) {
                try { $menu = Read-CpaConsoleForm 'CPA / 已保存的连接' @() @('载入已有配置', '重新配置', '退出') }
                catch [OperationCanceledException] { return }
                if ($menu.Action -eq 2) { return }
                $choice = [string]($menu.Action + 1)
            } else {
                $choice = Read-CpaSetting '1 载入已有配置  2 重新输入' {
                    param($Value) if ($Value -notin @('1','2')) { throw '请输入 1 或 2。' }; return $Value
                }
            }
        }
        if ($choice -eq '1') {
            try { $saved = Read-CpaConfig; $BaseUrl = $saved.base_url; $ProxyUrl = $saved.proxy_url }
            catch { Write-Host $_.Exception.Message }
        }
    }
    $prompted = $null -ne $saved -or -not $ProxyUrl -or -not $BaseUrl
    $connection = $null; $remember = $false
    if ($wantTui -and -not $saved -and ($prompted -or $noArguments -or $Tui)) {
        $initialKey = ''
        if ($KeyEnv) {
            if ($KeyEnv -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'Key 环境变量名称无效。' }
            $initialKey = [Environment]::GetEnvironmentVariable($KeyEnv)
        }
        try { $connection = Read-CpaConnection $ProxyUrl $BaseUrl $initialKey }
        catch [OperationCanceledException] { return }
        $initialKey = $null
        $ProxyUrl = $connection.Proxy; $BaseUrl = $connection.Base; $remember = $connection.Remember
    }
    if (-not $ProxyUrl) {
        $ProxyUrl = Read-CpaSetting '本机 HTTP/mixed 代理端口（如 7890）' {
            param($Value)
            if ($Value -cnotmatch '^[0-9]{1,5}$' -or [int]$Value -lt 1 -or [int]$Value -gt 65535) {
                throw '代理端口必须是 1–65535 的整数。'
            }
            return 'http://127.0.0.1:' + [int]$Value
        }
    }
    $proxy = ConvertTo-CpaProxy $ProxyUrl
    $base = if ($BaseUrl) { ConvertTo-CpaBase $BaseUrl } else {
        Read-CpaSetting 'HTTPS Base URL' { param($Value) ConvertTo-CpaBase $Value }
    }
    $uri = [Uri]$base
    # Refuse destinations that the platform would bypass even with an explicit proxy.
    if ([Net.WebProxy]::new($proxy, $false).IsBypassed($uri)) {
        throw '该入口会被系统绕过代理，已拒绝请求。请使用远程 HTTPS 域名。'
    }
    $useTui = if ($Once) { $false } elseif ($explicitTui) { [bool]$Tui } else {
        ($prompted -or $noArguments) -and $View -eq 'all' -and $Period -eq 'overview' -and $interactive
    }
    $key = $null; $secret = $null; $headers = $null; $consoleState = $null
    $protocol = [Net.ServicePointManager]::SecurityProtocol
    $views = if ($View -eq 'all') { @('usage', 'quota') } else { @($View) }
    try {
        if ($saved) { $key = $saved.api_key }
        elseif ($connection) { $key = $connection.Key; $connection.Key = $null }
        elseif ($KeyEnv) {
            if ($KeyEnv -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'Key 环境变量名称无效。' }
            $key = [Environment]::GetEnvironmentVariable($KeyEnv)
            if (-not $key) { throw '指定的 Key 环境变量未加载。' }
        } else {
            if (-not $interactive) { throw '隐藏输入需要交互终端；自动化请用 -KeyEnv 指定已有环境变量。' }
            $secret = Read-Host 'CPA API Key（隐藏输入）' -AsSecureString
            $key = [Net.NetworkCredential]::new('', $secret).Password
        }
        if ($key -cnotmatch '^[\x21-\x7e]{1,1024}$') { throw 'API Key 格式无效。' }
        $headers = @{ Authorization = 'Bearer ' + $key; Accept = 'application/json' }
        [Net.ServicePointManager]::SecurityProtocol = $protocol -bor [Net.SecurityProtocolType]::Tls12
        $refresh = $true; $selected = '1h'; $panel = 'overview'; $page = 0; $offset = 0; $notice = ''
        $offerSave = $interactive -and -not $saved -and -not $connection
        if ($useTui) {
            $consoleState = @([Console]::ForegroundColor, [Console]::BackgroundColor, [Console]::CursorVisible, [Console]::TreatControlCAsInput, [Console]::OutputEncoding)
            [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
            [Console]::TreatControlCAsInput = $true
            [Console]::Clear()
            $script:CpaFrameCache = @{}
        }
        do {
            if ($refresh) {
                if ($useTui) { Show-CpaConsoleFrame 'CPA / 用量与额度' @('正在通过 VPN 查询…') '查询中' }
                $results = @{}; $failures = @{}
                foreach ($item in $views) {
                    $scope = if ($item -eq 'usage') { 'own_key' } else { 'shared_pool' }
                    $path = if ($item -eq 'usage') { '/v1/usage?period=' + $Period } else { '/v1/quota' }
                    if ($item -eq 'usage' -and $Period -ne 'overview') { $path += '&recent=' + $Recent }
                    if ($item -eq 'usage' -and $Date) { $path += '&date=' + [Uri]::EscapeDataString($Date) }
                    try { $results[$item] = Get-CpaData $base $headers $path $scope $proxy }
                    catch { $failures[$item] = $_.Exception.Message }
                }
            }
            if ($useTui) {
                $lines = @(Get-CpaDashboardLines $results $failures $views $selected $page $panel ([Console]::WindowWidth - 1))
            } else {
                Write-Host 'CPA 只读看板 | VPN 代理'
                foreach ($item in $views) {
                    if ($failures.ContainsKey($item)) { Write-Host ("CPA ${item}：" + $failures[$item]); continue }
                    try {
                        if ($item -eq 'usage') { Show-Usage $results[$item] $selected $page $useTui } else { Show-Quota $results[$item] }
                    } catch {
                        $failures[$item] = '响应缺少所需字段，请更新 observer 或检查接口。'
                        Write-Host ("CPA ${item}：" + $failures[$item])
                    }
                    Write-Host ''
                }
            }
            if (-not $failures.Count -and $remember) {
                Save-CpaConfig $base ([string]$proxy) $key
                $remember = $false; $notice = '已保存连接配置。'
            }
            if (-not $failures.Count -and $offerSave) {
                $offerSave = $false
                if ($useTui) {
                    try { $menu = Read-CpaConsoleForm 'CPA / 保存连接（含 API Key）？' @() @('不保存', '保存') }
                    catch [OperationCanceledException] { break }
                    $answer = if ($menu.Action -eq 1) { 'y' } else { 'n' }
                } else {
                    $answer = Read-CpaSetting ('保存代理、Base URL 和 API Key 到 ' + (Get-CpaConfigPath) + '？[y/N]') { param($Value) return $Value.ToLowerInvariant() }
                }
                if ($answer -in @('y','yes')) {
                    Save-CpaConfig $base ([string]$proxy) $key
                    if ($useTui) { $notice = '已保存连接配置。' } else { Write-Host ('已保存：' + (Get-CpaConfigPath)) }
                }
            }
            if (-not $useTui) { break }
            $capacity = [Math]::Max(1, [Console]::WindowHeight - 4)
            $limit = [Math]::Max(0, $lines.Count - $capacity)
            $offset = [Math]::Min($offset, $limit)
            $colorState = if ($script:CpaColors) { '彩色' } else { '单色(C开启)' }
            $status = if ($failures.Count) { '部分查询失败' } else { '只读 · ' + $colorState }
            Show-CpaConsoleFrame ('CPA  用量看板 · ' + $status) @($lines | Select-Object -Skip $offset -First $capacity) 'Tab 视图  1-4 时段  N/P 翻页  ↑↓ 滚动  C 配色  R 刷新  Q 退出' -Notice $notice
            $action = Read-CpaDashboardAction
            $refresh = $action -in @('', 'r')
            if ($action -in @('1', '2', '3', '4')) { $selected = @('1h', '7d', '30d', 'all')[[int]$action - 1]; $page = 0 }
            elseif ($action -eq 'n') { $page++ } elseif ($action -eq 'p') { $page-- }
            elseif ($action -in @('left','right','tab','backtab')) {
                $panels = @('overview','models','quota'); $step = if ($action -in @('left','backtab')) { 2 } else { 1 }
                $panel = $panels[([array]::IndexOf($panels, $panel) + $step) % 3]; $offset = 0; $page = 0
            }
            elseif ($action -eq 'c') { $script:CpaColors = -not $script:CpaColors }
            elseif ($action -eq 'up') { $offset = [Math]::Max(0, $offset - 1) }
            elseif ($action -eq 'down') { $offset = [Math]::Min($limit, $offset + 1) }
            elseif ($action -eq 'pageup') { $offset = [Math]::Max(0, $offset - $capacity) }
            elseif ($action -eq 'pagedown') { $offset = [Math]::Min($limit, $offset + $capacity) }
            elseif ($action -eq 'home') { $offset = 0 }
            elseif ($action -eq 'end') { $offset = $limit }
        } while ($action -ne 'q')
        if ($failures.Count) { throw '部分查询失败，请检查上方提示。' }
    } finally {
        if ($consoleState) {
            [Console]::ForegroundColor = $consoleState[0]; [Console]::BackgroundColor = $consoleState[1]
            [Console]::CursorVisible = $consoleState[2]; [Console]::TreatControlCAsInput = $consoleState[3]
            [Console]::OutputEncoding = $consoleState[4]
            [Console]::Clear()
        }
        $key = $null
        if ($headers) { $headers.Clear() }
        if ($secret) { $secret.Dispose() }
        [Net.ServicePointManager]::SecurityProtocol = $protocol
    }
}

Invoke-CpaReport
