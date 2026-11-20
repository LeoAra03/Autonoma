@echo off
setlocal

rem Autonoma launcher: forwards all arguments to the PowerShell entry point.
set "AUTONOMA_HOME=%~dp0.."

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%AUTONOMA_HOME%\src\autonoma.ps1" %*

endlocal
