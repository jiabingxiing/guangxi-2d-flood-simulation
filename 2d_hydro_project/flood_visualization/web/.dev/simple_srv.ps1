$l = [System.Net.HttpListener]::new()
$l.Prefixes.Add("http://127.0.0.1:8794/")
$l.Start()
"started" | Out-File -Encoding utf8 "D:\NWKYYSJZX\2026年工作\1023 久隆 贵港 宁明 二维水动力模拟\2d_hydro_project\flood_visualization\web\.dev\simple_srv.log"
while ($true) { $c = $l.GetContext(); $c.Response.StatusCode = 200; $c.Response.Close() }