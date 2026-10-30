$ErrorActionPreference = "Stop"
$root = 'D:\NWKYYSJZX\2026年工作\1023 久隆 贵港 宁明 二维水动力模拟\2d_hydro_project\flood_visualization\outputs\web_bundle\v2-20261027'
$port = 8798
$l = [System.Net.HttpListener]::new()
$l.Prefixes.Add("http://127.0.0.1:$port/")
$l.Start()
Write-Host "LISTENING_OK"
$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
  $task = $l.GetContextAsync()
  if (-not $task.Wait(1000)) { continue }
  $ctx = $task.Result
  $rel = $ctx.Request.Url.LocalPath.TrimStart("/")
  $path = Join-Path $root ($rel -replace "/", "\")
  try {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $ctx.Response.ContentType = "application/octet-stream"
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
  } catch { $ctx.Response.StatusCode = 404 }
  $ctx.Response.Close()
}
$l.Stop(); $l.Close()
