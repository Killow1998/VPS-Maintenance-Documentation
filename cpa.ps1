# Public bootstrap only. The client download and all API calls require a proxy.
function Read-CpaProxyPort {
    if ($Host.Name -ne 'ConsoleHost' -or [Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        throw 'Run this launcher in an interactive terminal.'
    }
    while ($true) {
        $port = Read-Host 'Local HTTP/mixed proxy port (e.g. 7890)'
        if ($null -eq $port) { throw 'Input cancelled.' }
        if ($port -cmatch '^[1-9][0-9]{0,4}$' -and [int]$port -le 65535) { return [int]$port }
        Write-Host 'Enter an integer from 1 to 65535.'
    }
}

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

function Read-CpaSavedProxy {
    $path = Join-Path (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cpa') 'query.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    if ($Host.Name -ne 'ConsoleHost' -or [Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        throw 'Run this launcher in an interactive terminal.'
    }
    do { $choice = Read-Host '1 载入已有配置  2 重新输入' } while ($choice -notin @('1','2'))
    if ($choice -eq '2') { return $null }
    try {
        Test-CpaPrivatePath (Split-Path $path) $true
        Test-CpaPrivatePath $path
        if ((Get-Item -LiteralPath $path -Force).Length -gt 16384) { throw 'size' }
        $data = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
        if ($data.version -ne 1) { throw 'version' }
        return ([string](ConvertTo-CpaProxy $data.proxy_url))
    } catch { Write-Host '已存配置无法读取、权限不安全或内容无效；请重新输入。'; return $null }
}

function Start-CpaQuota {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $proxy = Read-CpaSavedProxy
    $choice = '1'
    if (-not $proxy) {
        $choice = '2'
        $port = Read-CpaProxyPort
        $proxy = "http://127.0.0.1:$port"
    }
    $url = 'https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/8335db5d27211d6af37704db85ec8efb0172b5c0/scripts/cpa/quota.ps1'
    $response = Invoke-WebRequest -UseBasicParsing -Uri $url -Proxy $proxy -MaximumRedirection 0 -TimeoutSec 60
    if ($response.StatusCode -ne 200) { throw 'Client download failed; no direct fallback.' }
    $bytes = $response.RawContentStream.ToArray()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    if ($digest -cne 'e401243da89da49acb154cbcbb644355cc737ff4e855eecb698eb6e53f7bb55d') {
        throw 'Client SHA256 mismatch; nothing was executed.'
    }
    $source = [Text.Encoding]::UTF8.GetString($bytes).TrimStart([char]0xFEFF)
    $previousChoice = $env:CPA_QUERY_CHOICE
    try {
        $env:CPA_QUERY_CHOICE = $choice
        & ([scriptblock]::Create($source)) -ProxyUrl $proxy -BaseUrl ''
    } finally { $env:CPA_QUERY_CHOICE = $previousChoice }
}

Start-CpaQuota
