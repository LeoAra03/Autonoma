@echo off
rem Arranca Autonoma en Windows sin Node y sin instalar nada a mano.
rem Doble clic = se abre la consola del agente. Con argumentos: Run-Autonoma.bat "resume mis notas"
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "BOOT=%~dp0scripts\bootstrap.py"

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD ( where python >nul 2>nul && set "PYCMD=python" )
if not defined PYCMD (
  echo No encontre Python 3.10 o mas nuevo.
  echo Descargalo de https://www.python.org/downloads/ y marca "Add python.exe to PATH".
  echo Alternativa: el ejecutable portable Autonoma.exe (no necesita Python). Ver INSTALL.md
  echo.
  pause
  exit /b 127
)

%PYCMD% "%BOOT%" run %*
set "RC=%ERRORLEVEL%"
if not "%AUTONOMA_NO_PAUSE%"=="1" (
  echo.
  pause
)
exit /b %RC%
