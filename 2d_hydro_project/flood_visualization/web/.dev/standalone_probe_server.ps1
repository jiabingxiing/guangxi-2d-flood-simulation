$root = 'D:\NWKYYSJZX\2026年工作\1023 久隆 贵港 宁明 二维水动力模拟\2d_hydro_project\flood_visualization\outputs\web_bundle\v2-20261027'
$port = 8796
$l = [System.Net.HttpListener]::new()
$l.Prefixes.Add("http://127.0.0.1:$port/")
$l.Start()
$mime = @{ '.js'='text/javascript; charset=utf-8'; '.css'='text/css; charset=utf-8'; '.json'='application/json; charset=utf-8'; '.geojson'='application/json; charset=utf-8'; '.bin'='application/octet-stream'; '.png'='image/png'; '.html'='text/html; charset=utf-8' }
$buf = New-Object byte[] 131072
while ($true) {
  $ctx = $l.GetContext()
  try {
    $rel = $ctx.Request.Url.LocalPath.TrimStart('/')
    if ([string]::IsNullOrEmpty($rel)) { $rel = 'index.html' }
    $path = [System.IO.Path]::Combine($root, ($rel -replace '/', '\'))
    if ([System.IO.File]::Exists($path)) {
      $ext = [System.IO.Path]::GetExtension($path).ToLower()
      $ct = $mime[$ext]
      if (-not $ct) { $ct = 'application/octet-stream' }
      $fs = [System.IO.File]::OpenRead($path)
      $ctx.Response.ContentType = $ct
      $ctx.Response.ContentLength64 = $fs.Length
      while (($n = $fs.Read($buf, 0, $buf.Length)) -gt 0) { $ctx.Response.OutputStream.Write($buf, 0, $n) }
      $fs.Dispose()
    } else {
      $ctx.Response.StatusCode = 404
    }
  } catch {
    [Console]::Error.WriteLine("ERR: " + $_.Exception.Message)
  }
  try { $ctx.Response.Close() } catch {}
}
