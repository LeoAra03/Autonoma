@echo off
rem arena-web\execute.bat - asistente web para arena.ai sobre un Brave que ya corre.
rem
rem ETAPA 1 (esta): diagnostico. No envia ni un prompt.
rem
rem   execute --probe               abre la pestana, recorre el DOM y te imprime
rem                                 que selector resolvio, cual fallo y que
rem                                 candidatos hay. Corriges selectors.toml y listo.
rem   execute --probe --json        lo mismo, en JSON.
rem   execute --config otro.toml    con otra configuracion.
rem
rem Antes, una sola vez, arranca Brave asi (dejalo vivo):
rem
rem   "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" ^
rem     --remote-debugging-port=9222 --remote-allow-origins=* ^
rem     --user-data-dir="%USERPROFILE%\.brave-autonoma" ^
rem     --disable-background-timer-throttling --disable-renderer-backgrounding ^
rem     --disable-backgrounding-occluded-windows ^
rem     --disable-features=CalculateNativeWinOcclusion https://arena.ai/agent
rem
rem Y en esa ventana inicia sesion y conecta GitHub. Esa es la pestana del bot:
rem puedes taparla con una pelicula, el bot trabaja por CDP sin tocar tu raton.

setlocal enabledelayedexpansion
cd /d "%~dp0"

if "%~1"=="" goto :uso
if /i "%~1"=="-h" goto :uso
if /i "%~1"=="--help" goto :uso

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo execute: necesito Python 3.10+ ^(https://www.python.org/downloads/^). 1>&2
  exit /b 127
)

%PY% "%~dp0probe.py" %*
exit /b %ERRORLEVEL%

:uso
for /f "usebackq delims=" %%A in (`findstr /b "rem" "%~f0"`) do echo %%~A
exit /b 0
