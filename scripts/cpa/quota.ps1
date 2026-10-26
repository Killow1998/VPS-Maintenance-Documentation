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
            if ([Environment]::GetEnvironmentVariable('NO_COLOR') -eq $null) {
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

function Invoke-CpaReport {
    $ErrorActionPreference = 'Stop'
    if ($Once -and $Tui) { throw 'Once 和 Tui 不能同时使用。' }
    if ($Date -and $Period -notin @('day', 'month')) { throw 'Date 仅支持 day/month。' }
    $interactive = Test-CpaTerminal
    if ($Tui -and ($View -ne 'all' -or $Period -ne 'overview' -or -not $interactive)) {
        throw 'TUI 需要交互终端且使用默认 all/overview；单次查询请用 -Once。'
    }
    $prompted = -not $ProxyUrl -or -not $BaseUrl
    if (-not $ProxyUrl) {
        $ProxyUrl = Read-CpaSetting '本机 HTTP/mixed 代理端口（如 7890）' {
            param($Value)
            if ($Value -cnotmatch '^[0-9]{1,5}$' -or [int]$Value -lt 1 -or [int]$Value -gt 65535) {
                throw '代理端口必须是 1–65535 的整数。'
            }
            return 'http://127.0.0.1:' + [int]$Value
        }
    }
    $proxy = $null
    if (-not [Uri]::TryCreate($ProxyUrl, [UriKind]::Absolute, [ref]$proxy) -or $proxy.Scheme -ne 'http' -or
        $proxy.Host -notin @('127.0.0.1', '[::1]') -or $proxy.Port -le 0 -or
        $proxy.Authority -notmatch ':\d+$' -or $proxy.AbsolutePath -ne '/' -or
        $proxy.UserInfo -or $proxy.Query -or $proxy.Fragment -or $ProxyUrl -match '\s') {
        throw '必须用 -ProxyUrl 或 CPA_PROXY 指定本机 HTTP/mixed 代理，例如 http://127.0.0.1:7890；禁止直连。'
    }
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
    $key = $null; $secret = $null; $headers = $null
    $protocol = [Net.ServicePointManager]::SecurityProtocol
    $views = if ($View -eq 'all') { @('usage', 'quota') } else { @($View) }
    try {
        if ($KeyEnv) {
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
        $refresh = $true; $selected = '1h'; $page = 0
        do {
            if ($refresh) {
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
            if ($useTui) { Clear-Host }
            Write-Host 'CPA 只读看板 | 强制本机代理 | Clash 出站仍须选择真实节点'
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
            if (-not $useTui) { break }
            do { $action = Read-Host '1/2/3/4: 1h/7d/30d/全部 · N/P 模型翻页 · R/Enter 刷新 · Q 退出' }
            while ($null -ne $action -and $action.Trim().ToLowerInvariant() -notin @('', 'r', 'q', '1', '2', '3', '4', 'n', 'p'))
            if ($null -eq $action) { break }
            $action = $action.Trim().ToLowerInvariant()
            $refresh = $action -in @('', 'r')
            if ($action -in @('1', '2', '3', '4')) { $selected = @('1h', '7d', '30d', 'all')[[int]$action - 1]; $page = 0 }
            elseif ($action -eq 'n') { $page++ } elseif ($action -eq 'p') { $page-- }
        } while ($action -ne 'q')
        if ($failures.Count) { throw '部分查询失败，请检查上方提示。' }
    } finally {
        $key = $null
        if ($headers) { $headers.Clear() }
        if ($secret) { $secret.Dispose() }
        [Net.ServicePointManager]::SecurityProtocol = $protocol
    }
}

Invoke-CpaReport
