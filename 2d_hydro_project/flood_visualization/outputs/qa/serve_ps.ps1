# Prototype: PowerShell HttpListener static server (no Python needed).
# qa feasibility probe only; lives under outputs/qa/** and writes no files.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File serve_ps.ps1 -Root <bundle> -Port 8793
# Binds 127.0.0.1 only. Pure ASCII on purpose: PS 5.1 parses BOM-less UTF-8 as GBK.
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [int]$Port = 8793
)

$ErrorActionPreference = 'Stop'
$RootFull = (Resolve-Path -LiteralPath $Root).Path
$prefix = "http://127.0.0.1:$Port/"

$mime = @{
    '.html'    = 'text/html; charset=utf-8'
    '.htm'     = 'text/html; charset=utf-8'
    '.js'      = 'application/javascript; charset=utf-8'
    '.mjs'     = 'application/javascript; charset=utf-8'
    '.css'     = 'text/css; charset=utf-8'
    '.json'    = 'application/json; charset=utf-8'
    '.png'     = 'image/png'
    '.jpg'     = 'image/jpeg'
    '.svg'     = 'image/svg+xml'
    '.geojson' = 'application/geo+json; charset=utf-8'
    '.bin'     = 'application/octet-stream'
    '.txt'     = 'text/plain; charset=utf-8'
    '.md'      = 'text/plain; charset=utf-8'
    '.woff2'   = 'font/woff2'
}

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add($prefix)
$listener.Start()
Write-Host "[serve_ps] listening $prefix root=$RootFull"

try {
    while ($listener.IsListening) {
        $ctx = $listener.GetContext()
        $req = $ctx.Request
        $res = $ctx.Response
        try {
            $rel = [System.Uri]::UnescapeDataString($req.Url.AbsolutePath).TrimStart('/')
            if ([string]::IsNullOrWhiteSpace($rel)) { $rel = 'index.html' }
            $rel = $rel -replace '/', [System.IO.Path]::DirectorySeparatorChar
            $full = [System.IO.Path]::GetFullPath((Join-Path $RootFull $rel))
            if (-not $full.StartsWith($RootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                $res.StatusCode = 403
                $res.Close()
                continue
            }
            if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
                $res.StatusCode = 404
                $bytes404 = [System.Text.Encoding]::UTF8.GetBytes('404 not found')
                $res.OutputStream.Write($bytes404, 0, $bytes404.Length)
                $res.Close()
                continue
            }
            $ext = [System.IO.Path]::GetExtension($full).ToLowerInvariant()
            $ct = $mime[$ext]
            if (-not $ct) { $ct = 'application/octet-stream' }
            $res.StatusCode = 200
            $res.ContentType = $ct
            $res.Headers.Add('Cache-Control', 'no-cache')
            $bytes = [System.IO.File]::ReadAllBytes($full)
            $res.ContentLength64 = $bytes.Length
            $res.OutputStream.Write($bytes, 0, $bytes.Length)
        }
        catch {
            try { $res.StatusCode = 500 } catch { }
        }
        finally {
            try { $res.Close() } catch { }
        }
    }
}
finally {
    $listener.Stop()
    $listener.Close()
    Write-Host '[serve_ps] stopped'
}
