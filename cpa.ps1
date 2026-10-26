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

function Start-CpaQuota {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $port = Read-CpaProxyPort
    $proxy = "http://127.0.0.1:$port"
    $url = 'https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/8335db5d27211d6af37704db85ec8efb0172b5c0/scripts/cpa/quota.ps1'
    $response = Invoke-WebRequest -UseBasicParsing -Uri $url -Proxy $proxy -MaximumRedirection 0 -TimeoutSec 60
    if ($response.StatusCode -ne 200) { throw 'Client download failed; no direct fallback.' }
    $bytes = $response.RawContentStream.ToArray()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    if ($digest -cne '3bfe16b54fc2953740c470b26898ef08147fbafa23b021beaef290fbb2ced6ee') {
        throw 'Client SHA256 mismatch; nothing was executed.'
    }
    $source = [Text.Encoding]::UTF8.GetString($bytes).TrimStart([char]0xFEFF)
    & ([scriptblock]::Create($source)) -ProxyUrl $proxy -BaseUrl ''
}

Start-CpaQuota
