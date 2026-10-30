@echo off
REM ---------------------------------------------------------------
REM floodviz local launcher (ASCII only).
REM Serves the newest web bundle on http://127.0.0.1:8765 only.
REM Loopback only: never binds 0.0.0.0, never opens LAN access.
REM
REM Usage:
REM   start_local.cmd                 serve newest bundle in outputs\web_bundle
REM   start_local.cmd v3-20261028     serve the named bundle
REM   set PORT=8766 & start_local.cmd use another loopback port
REM ---------------------------------------------------------------
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0.."
set "BUNDLE_ROOT=%ROOT%\outputs\web_bundle"
if "%PORT%"=="" set "PORT=8765"

if not "%~1"=="" (
  set "BUNDLE=%BUNDLE_ROOT%\%~1"
) else (
  rem 只选择真正可服务的发布包，跳过 .building-*、smoke、诊断目录。
  for /f "delims=" %%D in ('dir /b /ad /o-d "%BUNDLE_ROOT%" 2^>nul') do (
    if exist "%BUNDLE_ROOT%\%%D\index.html" if exist "%BUNDLE_ROOT%\%%D\manifest.json" if not defined BUNDLE set "BUNDLE=%BUNDLE_ROOT%\%%D"
  )
)

if not defined BUNDLE (
  echo [floodviz] ERROR: no complete bundle found under "%BUNDLE_ROOT%".
  echo [floodviz] For zero-install use: standalone\广西二维洪涝可视化_双击打开\打开可视化.cmd
  exit /b 1
)

if not exist "%BUNDLE%\index.html" goto :incomplete
if not exist "%BUNDLE%\manifest.json" goto :incomplete
goto :bundle_ok

:incomplete
echo [floodviz] ERROR: bundle is incomplete: "%BUNDLE%".
echo [floodviz] For zero-install use: standalone\广西二维洪涝可视化_双击打开\打开可视化.cmd
exit /b 1

:bundle_ok
where python >nul 2>nul
if errorlevel 1 (
  echo [floodviz] ERROR: python not found in PATH.
  echo [floodviz] Zero-install fallback: standalone\广西二维洪涝可视化_双击打开\打开可视化.cmd
  exit /b 1
)

set "PYTHONUTF8=1"
echo [floodviz] bundle : "%BUNDLE%"
echo [floodviz] bind   : 127.0.0.1 only (loopback)

rem 端口占用时提示，不先打开错误浏览器页面。
set /a LAST_PORT=%PORT%+10 >nul
for /l %%P in (%PORT%,1,%LAST_PORT%) do (
  python -c "import socket,sys;s=socket.socket();s.settimeout(.2);r=s.connect_ex(('127.0.0.1',%%P));s.close();sys.exit(0 if r else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PORT=%%P"
    goto :serve
  )
)
echo [floodviz] ERROR: ports %PORT%-%LAST_PORT% are busy.
echo [floodviz] Try: set PORT=8766 ^& start_local.cmd
exit /b 1

:serve
echo [floodviz] URL    : http://127.0.0.1:%PORT%/index.html
echo [floodviz] Press Ctrl+C to stop.
echo.
start "" "http://127.0.0.1:%PORT%/index.html"
python -m http.server %PORT% --bind 127.0.0.1 --directory "%BUNDLE%"
endlocal
