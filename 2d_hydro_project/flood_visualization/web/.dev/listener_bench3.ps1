$root = 'D:\NWKYYSJZX\2026年工作\1023 久隆 贵港 宁明 二维水动力模拟\2d_hydro_project\flood_visualization\outputs\web_bundle\v2-20261027'
$port = 8797
$l = [System.Net.HttpListener]::new()
$l.Prefixes.Add("http://127.0.0.1:$port/")
$l.Start()
while ($true) {
  $ctx = $l.GetContext()
  try {
    $rel = $ctx.Request.Url.LocalPath.TrimStart("/")
    $path = Join-Path $root ($rel -replace "/", "\")
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $ctx.Response.ContentLength64 = $bytes.Length
    $ctx.Response.ContentType = "application/octet-stream"
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
  } catch {
    try { $ctx.Response.StatusCode = 404 } catch {}
  }
  $ctx.Response.Close()
}
