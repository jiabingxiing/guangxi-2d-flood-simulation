$ErrorActionPreference = "Stop"
$root = 'D:\NWKYYSJZX\2026年工作\1023 久隆 贵港 宁明 二维水动力模拟\2d_hydro_project\flood_visualization\outputs\web_bundle\v2-20261027'
$port = 8799
$l = [System.Net.HttpListener]::new()
$l.Prefixes.Add("http://127.0.0.1:$port/")
$l.Start()
Write-Host "LISTENING_OK"
for ($i = 0; $i -lt 3; $i++) {
  $ctx = $l.GetContext()
  $rel = $ctx.Request.Url.LocalPath.TrimStart("/")
  if ([string]::IsNullOrEmpty($rel)) { $rel = "index.html" }
  $path = Join-Path $root ($rel -replace "/", "\")
  if (Test-Path $path) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $ctx.Response.ContentType = "application/octet-stream"
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    Write-Host ("SERVED " + $bytes.Length + " bytes for " + $rel)
  } else {
    $ctx.Response.StatusCode = 404
    Write-Host ("MISS " + $rel)
  }
  $ctx.Response.Close()
}
$l.Stop(); $l.Close()
Write-Host "DONE"
