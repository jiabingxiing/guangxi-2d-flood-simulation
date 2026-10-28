param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

# Standalone flood visualization server.
# Windows PowerShell 5.1 compatible. ASCII only on purpose.
$ErrorActionPreference = 'Stop'
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$RequestedPort = $Port
$Listener = $null
$Prefix = $null

# Try the requested loopback port, then ten fallback ports.
for ($PortOffset = 0; $PortOffset -le 10; $PortOffset++) {
    $CandidatePort = $RequestedPort + $PortOffset
    $CandidatePrefix = "http://127.0.0.1:$CandidatePort/"
    $CandidateListener = New-Object System.Net.HttpListener
    $CandidateListener.Prefixes.Add($CandidatePrefix)
    try {
        $CandidateListener.Start()
        $Listener = $CandidateListener
        $Port = $CandidatePort
        $Prefix = $CandidatePrefix
        break
    } catch {
        $CandidateListener.Close()
    }
}

if ($null -eq $Listener) {
    Write-Host "Cannot start a local server on ports $RequestedPort-$($RequestedPort + 10)."
    Write-Host "Close another local server or run: powershell -File server.ps1 -Port 8766"
    exit 2
}

$Mime = @{
    '.html' = 'text/html; charset=utf-8'
    '.htm'  = 'text/html; charset=utf-8'
    '.js'   = 'text/javascript; charset=utf-8'
    '.css'  = 'text/css; charset=utf-8'
    '.json' = 'application/json; charset=utf-8'
    '.geojson' = 'application/geo+json; charset=utf-8'
    '.txt'  = 'text/plain; charset=utf-8'
    '.svg'  = 'image/svg+xml'
    '.png'  = 'image/png'
    '.jpg'  = 'image/jpeg'
    '.jpeg' = 'image/jpeg'
    '.webp' = 'image/webp'
    '.gif'  = 'image/gif'
    '.bin'  = 'application/octet-stream'
    '.gz'   = 'application/gzip'
    '.woff' = 'font/woff'
    '.woff2'= 'font/woff2'
}

function Get-SafeFilePath([string]$UrlPath) {
    $relative = [System.Uri]::UnescapeDataString($UrlPath)
    $relative = $relative.TrimStart('/')
    if ([string]::IsNullOrWhiteSpace($relative)) { $relative = 'index.html' }
    $relative = $relative.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
    $candidate = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($Root, $relative))
    $rootWithSep = $Root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($rootWithSep, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $null
    }
    return $candidate
}

function Send-FileResponse($Context, [string]$FilePath) {
    $Response = $Context.Response
    $Extension = [System.IO.Path]::GetExtension($FilePath).ToLowerInvariant()
    if ($Mime.ContainsKey($Extension)) { $Response.ContentType = $Mime[$Extension] }
    $Response.StatusCode = 200
    $Response.ContentLength64 = ([System.IO.FileInfo]$FilePath).Length
    $Response.Headers['Cache-Control'] = 'no-cache'
    if ($Context.Request.HttpMethod -eq 'HEAD') {
        $Response.Close()
        return
    }
    $Input = New-Object System.IO.FileStream($FilePath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        $Buffer = New-Object byte[] (131072)
        while (($Count = $Input.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
            $Response.OutputStream.Write($Buffer, 0, $Count)
        }
    } finally {
        $Input.Dispose()
        $Response.Close()
    }
}

function Send-TextResponse($Context, [int]$Status, [string]$Text) {
    $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $Context.Response.StatusCode = $Status
    $Context.Response.ContentType = 'text/plain; charset=utf-8'
    $Context.Response.ContentLength64 = $Bytes.Length
    if ($Context.Request.HttpMethod -ne 'HEAD') {
        $Context.Response.OutputStream.Write($Bytes, 0, $Bytes.Length)
    }
    $Context.Response.Close()
}

$url = $Prefix + 'index.html'
Write-Host "Flood visualization is ready: $url"
Write-Host "Root: $Root"
Write-Host 'Only 127.0.0.1 is used. Close this window to stop the server.'
if (-not $NoBrowser) { Start-Process $url }

try {
    while ($Listener.IsListening) {
        $Context = $Listener.GetContext()
        try {
            $FilePath = Get-SafeFilePath $Context.Request.Url.AbsolutePath
            if ($null -eq $FilePath -or -not [System.IO.File]::Exists($FilePath)) {
                Send-TextResponse $Context 404 '404 Not Found'
            } else {
                Send-FileResponse $Context $FilePath
            }
        } catch {
            try { Send-TextResponse $Context 500 '500 Internal Server Error' } catch { }
        }
    }
} finally {
    $Listener.Stop()
    $Listener.Close()
}
