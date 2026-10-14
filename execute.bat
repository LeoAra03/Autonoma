@echo off
rem ./execute.bat "enunciado" --hasta-que-pare
rem
rem Lanza el lazo supervisor de Autonoma sobre un objetivo y lo deja trabajando solo:
rem encadena turnos, escribe lo que hace en supervisor\turns.jsonl y se detiene cuando
rem el modelo dice DONE, cuando se le acaba el presupuesto o cuando lo paras.
rem
rem   execute "deja la tanda verde"
rem   execute "refactoriza el modulo de auth" --hasta-que-pare
rem   execute "refactoriza el modulo de auth" --hasta-que-pare --max 15
rem   execute "sigue donde ibamos" --hasta-que-pare --continuar
rem
rem Como se para (sin romper nada):
rem   Ctrl+C                    termina al acabar el turno en curso
rem   supervisor\STOP           desde otra terminal (o desde el panel del celular)
rem   supervisor\steer.txt      le das otra instruccion sin pararlo
rem
rem Atajos que anade este wrapper (lo demas pasa tal cual a Autonoma):
rem   --max N   = --turns N     tope de iteraciones (con --hasta-que-pare no hace falta)
rem   --sin-panel               no abrir el panel
rem   -h, --help               esta ayuda
rem
rem Por defecto abre el panel en este equipo. Para verlo desde el celular:
rem   execute "objetivo" --hasta-que-pare --panel 0.0.0.0

setlocal enabledelayedexpansion
cd /d "%~dp0"

if "%~1"=="" goto :uso
if /i "%~1"=="-h" goto :uso
if /i "%~1"=="--help" goto :uso

set "ARGS="
set "PANEL=1"
set "DADO_PANEL=0"
set "ENUNCIADO=0"

:loop
if "%~1"=="" goto :fin
if /i "%~1"=="--sin-panel" ( set "PANEL=0" & shift & goto :loop )
if /i "%~1"=="--max" (
  if "%~2"=="" ( echo execute: --max necesita un numero ^(--max 15^) 1>&2 & exit /b 2 )
  set "ARGS=!ARGS! --turns "%~2""
  shift
  shift
  goto :loop
)
if /i "%~1"=="--panel" ( set "DADO_PANEL=1" & set "ARGS=!ARGS! "%~1"" & shift & goto :loop )
if /i "%~1"=="--panel-port" ( set "DADO_PANEL=1" & set "ARGS=!ARGS! "%~1"" & shift & goto :loop )
if "%~1:~0,6%"=="--max=" (
  set "ARGS=!ARGS! --turns "%~1:~6%""
  shift
  goto :loop
)
set "ARGS=!ARGS! "%~1""
set "CABEZA=%~1"
if not "%CABEZA:~0,1%"=="-" set "ENUNCIADO=1"
shift
goto :loop

:fin
if "%ENUNCIADO%"=="0" (
  echo execute: falta el enunciado. Ejemplo: execute "deja la tanda verde" --hasta-que-pare 1>&2
  exit /b 2
)
if "%PANEL%"=="1" if "%DADO_PANEL%"=="0" set "ARGS=%ARGS% --panel"

if exist "Autonoma.exe" (
  Autonoma.exe --supervise %ARGS%
) else (
  where py >nul 2>nul && ( py -3 scripts\bootstrap.py run --supervise %ARGS% ) || ( python scripts\bootstrap.py run --supervise %ARGS% )
)
exit /b %ERRORLEVEL%

:uso
for /f "usebackq delims=" %%A in (`findstr /b "rem" "%~f0"`) do echo %%~A
exit /b 0
