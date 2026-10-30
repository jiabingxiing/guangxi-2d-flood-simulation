$ErrorActionPreference = "Stop"
$port = 8799
try {
  $l = [System.Net.HttpListener]::new()
  $l.Prefixes.Add("http://127.0.0.1:$port/")
  $l.Start()
  Write-Host "HTTPLISTENER_OK port=$port"
  $l.Stop(); $l.Close()
} catch {
  Write-Host ("HTTPLISTENER_FAIL: " + $_.Exception.GetType().FullName + " :: " + $_.Exception.Message)
}
Write-Host ("IsAdmin=" + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
