# Standalone client for Windows PowerShell 5.1+ and PowerShell 7+.
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$BaseUrl = $env:CPA_BASEURL,
    [string]$KeyEnv
)

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
    $text = ''; $width = 0
    foreach ($char in $label.ToCharArray()) {
        $step = if ([int]$char -ge 0x2e80) { 2 } else { 1 }
        if ($width + $step -gt 16) { break }
        $text += $char; $width += $step
    }
    return $text + (' ' * (16 - $width))
}

function Get-Remaining($Value) {
    if ($null -eq $Value -or $Value -is [bool] -or $Value -is [string]) { return $null }
    try { $number = [double]$Value } catch { return $null }
    if ([double]::IsNaN($number) -or [double]::IsInfinity($number) -or $number -lt 0 -or $number -gt 100) {
        return $null
    }
    return $number
}

function Invoke-CpaQuota {
    $ErrorActionPreference = 'Stop'
    $base = $BaseUrl.TrimEnd('/')
    if ($base.EndsWith('/v1')) { $base = $base.Substring(0, $base.Length - 3) }
    $uri = $null
    if (-not [Uri]::TryCreate($base, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne 'https' -or
        -not $uri.Host -or $uri.Port -eq 0 -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $base -match '\s') {
        throw '请提供 HTTPS Base URL，不包含用户名、密码、查询参数或片段。'
    }
    $key = $null; $secret = $null; $headers = $null
    $protocol = [Net.ServicePointManager]::SecurityProtocol
    try {
        if ($KeyEnv) {
            if ($KeyEnv -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw 'Key 环境变量名称无效。' }
            $key = [Environment]::GetEnvironmentVariable($KeyEnv)
            if (-not $key) { throw '指定的 Key 环境变量未加载。' }
        } else {
            $secret = Read-Host 'CPA API Key（隐藏输入）' -AsSecureString
            $key = [Net.NetworkCredential]::new('', $secret).Password
        }
        if ($key -cnotmatch '^[\x21-\x7e]{1,1024}$') { throw 'API Key 格式无效。' }
        $headers = @{ Authorization = 'Bearer ' + $key; Accept = 'application/json' }
        [Net.ServicePointManager]::SecurityProtocol = $protocol -bor [Net.SecurityProtocolType]::Tls12
        try {
            $data = Invoke-RestMethod -Uri ($base + '/v1/quota') -Headers $headers -Method Get `
                -UseBasicParsing -MaximumRedirection 0 -TimeoutSec 30 -ErrorAction Stop
        } catch {
            $status = 0
            if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
            $message = switch ($status) {
                401 { 'Key 无效或已撤销' }
                404 { '入口未配置 /v1/quota' }
                429 { '查询过于频繁，请稍后再试' }
                default { '请检查网络、TLS 和入口服务；脚本不跟随重定向' }
            }
            throw "额度查询失败（HTTP $status）：$message。"
        }
        if ($data.scope -cne 'shared_pool' -or $data.accounts -isnot [Array]) {
            throw '此入口未返回匿名号池，请检查 /v1/quota 路由。'
        }
    } finally {
        $key = $null
        if ($headers) { $headers.Clear() }
        if ($secret) { $secret.Dispose() }
        [Net.ServicePointManager]::SecurityProtocol = $protocol
    }

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
            if ([Environment]::GetEnvironmentVariable('NO_COLOR') -eq $null) {
                Write-Host "$bar $percent" -ForegroundColor $color -NoNewline
            } else { Write-Host "$bar $percent" -NoNewline }
            Write-Host ('  重置 ' + (Get-ResetLabel $row.Value.resets_at))
        }
    }
    Write-Host "`n█ 剩余  ░ 已用 · 时间为北京时间 · 灰色=旧值/未知 · 各账号百分比不可相加"
}

Invoke-CpaQuota
